"""归档原文件：计算指纹，清理同名旧内容，按文件名目录写入 MinIO。"""

import asyncio
import hashlib
from pathlib import Path

from common.errors import OperationError
from common.logging.logger import logger, node_log, step_log
from processor.import_processor.state import DataDict, ImportState, SourceFile
from utils.node_execution import ImportDependencies
from utils.clients.file_replacement import remove_previous_file
from utils.storage_keys import file_prefix
from utils.task_utils import add_done_task, add_running_task

def _calculate_fingerprint(file_path: Path) -> str:
    """同步分块读取原始字节，计算 SHA-256；由准备步骤在线程中调用。"""
    digest = hashlib.sha256()
    with file_path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


@step_log("step_1_prepare_source_file")
async def step_1_prepare_source_file(state: ImportState) -> SourceFile:
    """生成来源信息；同名文件使用同一 MinIO 目录与成功清单键。"""
    if not state.get("task_claimed"):
        raise OperationError("TASK_NOT_CLAIMED", "归档原文件前必须由 entry 领取任务")

    file_path = Path(state["file_path"])
    source_name = state["upload_metadata"].get("source_file_name")
    file_name = source_name if isinstance(source_name, str) and source_name.strip() else file_path.name
    source_format = file_path.suffix.lower()[1:]
    # 保留扩展名区分同名 MD/PDF/DOCX，不按任务创建多份文档目录。
    prefix = file_prefix(file_name)
    # 文件 I/O 在线程执行，不解析正文、不把整个文件加载到内存。
    fingerprint = await asyncio.to_thread(_calculate_fingerprint, file_path)
    return {
        "document_id": state["document_id"],
        "file_name": file_name,
        "object_key": f"{prefix}/source.{source_format}",
        "format": source_format,
        "fingerprint": fingerprint,
        "import_manifest_key": f"{prefix}/manifest.json",
    }


@step_log("step_2_remove_previous_file")
async def step_2_remove_previous_file(source_file: SourceFile, *, deps: ImportDependencies) -> None:
    """先删除四集合旧行与旧对象，失败直接终止，不留下旧图片/清单。"""
    await asyncio.to_thread(remove_previous_file, source_file["file_name"],
        milvus_client=deps.milvus_client, minio_client=deps.minio_client,
        bucket_name=deps.minio_bucket_name)


@step_log("step_3_upload_source_file")
async def step_3_upload_source_file(
    state: ImportState, source_file: SourceFile, *, deps: ImportDependencies,
) -> None:
    """使用注入客户端上传原文件；桶由应用装配层准备，失败直接抛错。"""
    if (deps.minio_client is None or not isinstance(deps.minio_bucket_name, str)
            or not deps.minio_bucket_name.strip()):
        raise OperationError("MISSING_STORAGE_DEPENDENCY", "归档原文件必须注入 MinIO 客户端和桶名")

    object_key = source_file["object_key"]
    # SDK 的同步文件上传在线程中执行；异常交由 bind_node 登记失败进度。
    try:
        await asyncio.to_thread(
            deps.minio_client.fput_object,
            bucket_name=deps.minio_bucket_name,
            object_name=object_key,
            file_path=state["file_path"],
        )
    except OperationError:
        raise
    except Exception as exc:
        # SDK 异常统一转成业务错误，避免接口层只看到笼统的 NODE_EXECUTION_FAILED。
        logger.error("原文件上传失败：{}", type(exc).__name__)
        raise OperationError(
            "SOURCE_UPLOAD_FAILED", "原文件上传失败，请检查 MinIO 桶、凭据与服务状态",
            retryable=True,
        ) from exc


@node_log("node_save_file")
async def node_save_file(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    """组织原文件归档步骤，全部成功后才登记完成并返回来源信息增量。"""
    # 1. 添加节点到运行时列表，沿用 entry 已领取的任务。
    started_at = await add_running_task(state, "save_file", tasks=deps.tasks)

    # 2. 获取来源信息，计算文件指纹和稳定对象键。
    source_file = await step_1_prepare_source_file(state)

    # 3. 清理同名文件的旧向量、原文件、图片、外置记录与成功清单。
    await step_2_remove_previous_file(source_file, deps=deps)

    # 4. 上传新原文件到以文件名命名的 MinIO 目录。
    await step_3_upload_source_file(state, source_file, deps=deps)

    # 5. 全部成功后添加节点到已完成列表。
    await add_done_task(state, "save_file", tasks=deps.tasks, started_at=started_at)

    # 6. 返回 source_file 增量。
    return {"source_file": source_file}
