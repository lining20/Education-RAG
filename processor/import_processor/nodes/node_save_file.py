"""归档原文件：分块计算指纹、上传至 MinIO，返回来源信息增量。"""

import asyncio
import hashlib
from pathlib import Path

from common.errors import OperationError
from common.logging.logger import node_log, step_log
from processor.import_processor.state import DataDict, ImportState, SourceFile
from utils.node_execution import ImportDependencies
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
    """从 entry 成功后的 State 生成六字段来源信息，不修改输入。

    文件格式取已校验路径的后缀；显示文件名优先取上传参数中的非空字符串。
    对象键由任务、文档及格式决定，同任务重试保持稳定；此处不写成功清单。
    """
    if not state.get("task_claimed"):
        raise OperationError("TASK_NOT_CLAIMED", "归档原文件前必须由 entry 领取任务")

    file_path = Path(state["file_path"])
    source_name = state["upload_metadata"].get("source_file_name")
    file_name = source_name if isinstance(source_name, str) and source_name.strip() else file_path.name
    source_format = file_path.suffix.lower()[1:]
    prefix = f"imports/{state['task_id']}/{state['document_id']}"
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


@step_log("step_2_upload_source_file")
async def step_2_upload_source_file(
    state: ImportState, source_file: SourceFile, *, deps: ImportDependencies,
) -> None:
    """将原路径文件上传到注入的既有桶；成功无返回值，失败直接抛错。

    输入为 State、准备好的来源信息和存储依赖。不创建客户端、桶或权限，
    不删除对象，不上传 manifest；调用方仅在上传成功后返回 source_file。
    """
    if (deps.minio_client is None or not isinstance(deps.minio_bucket_name, str)
            or not deps.minio_bucket_name.strip()):
        raise OperationError("MISSING_STORAGE_DEPENDENCY", "归档原文件必须注入 MinIO 客户端和桶名")

    # SDK 的同步文件上传在线程中执行；异常交由 bind_node 登记失败进度。
    await asyncio.to_thread(
        deps.minio_client.fput_object,
        bucket_name=deps.minio_bucket_name,
        object_name=source_file["object_key"],
        file_path=state["file_path"],
    )


@node_log("node_save_file")
async def node_save_file(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    """组织原文件归档步骤，全部成功后才登记完成并返回来源信息增量。"""
    # 1. 添加节点到运行时列表，沿用 entry 已领取的任务。
    started_at = await add_running_task(state, "save_file", tasks=deps.tasks)

    # 2. 获取来源信息，计算文件指纹和稳定对象键。
    source_file = await step_1_prepare_source_file(state)

    # 3. 上传原文件到 MinIO。
    await step_2_upload_source_file(state, source_file, deps=deps)

    # 4. 全部成功后添加节点到已完成列表。
    await add_done_task(state, "save_file", tasks=deps.tasks, started_at=started_at)

    # 5. 返回 source_file 增量。
    return {"source_file": source_file}


if __name__ == "__main__":
    from copy import deepcopy
    from tempfile import TemporaryDirectory

    from common.logging.logger import init_logger, logger
    from processor.import_processor.nodes.node_entry import node_entry
    from processor.import_processor.state import create_initial_state
    from utils.node_execution import bind_node
    from utils.task_utils import ImportTaskStore

    class OfflineMinio:
        """只记录上传参数或抛错的简短替身，不连接真实 MinIO。"""

        def __init__(self, fail=False):
            self.fail = fail
            self.uploads = []

        def fput_object(self, *, bucket_name, object_name, file_path):
            self.uploads.append((bucket_name, object_name, file_path))
            if self.fail:
                raise OperationError("SOURCE_UPLOAD_FAILED", "离线上传替身模拟失败")

    async def self_check():
        logger.info("node_save_file 自测开始：3 个案例，上传使用离线替身")
        with TemporaryDirectory(prefix="node_save_file_") as directory:
            file_path = Path(directory) / "sample.md"
            content = b"archive check\n" * 100_000  # 超过一个读取块，检查分块指纹。
            file_path.write_bytes(content)
            tasks = ImportTaskStore()
            cases = (("归档成功", False), ("上传失败", True), ("同任务对象键稳定", False))
            for index, (description, fail) in enumerate(cases, 1):
                logger.info("案例 {}/3：{}", index, description)
                client = OfflineMinio(fail=fail)
                deps = ImportDependencies(tasks=tasks, minio_client=client, minio_bucket_name="offline-bucket")
                task_id, document_id = f"archive-test-{index}", f"archive-doc-{index}"
                tasks.create_pending(task_id, document_id)
                state = create_initial_state(
                    task_id=task_id, document_id=document_id, file_path=str(file_path),
                    upload_metadata={"source_file_name": "课程资料.md"} if index == 1 else {},
                )
                # 通过真实 entry 建立前序完成状态，不修改工具内部任务字典。
                state.update(await bind_node("entry", node_entry, deps)(state))
                assert tasks.get(task_id)["completed_nodes"] == ["entry"]
                before = deepcopy(state)
                update = await bind_node("save_file", node_save_file, deps)(state)
                progress = tasks.get(task_id)
                assert state == before and progress["running_nodes"] == []
                assert client.uploads == [("offline-bucket", f"imports/{task_id}/{document_id}/source.md", str(file_path))]
                if fail:
                    assert update["status"] == "failed" and update["error"]["code"] == "SOURCE_UPLOAD_FAILED"
                    assert "source_file" not in update and progress["completed_nodes"] == ["entry"]
                    assert progress["failed_nodes"] == ["save_file"]
                else:
                    source = update["source_file"]
                    assert source == {
                        "document_id": document_id,
                        "file_name": "课程资料.md" if index == 1 else "sample.md",
                        "object_key": f"imports/{task_id}/{document_id}/source.md",
                        "format": "md",
                        "fingerprint": hashlib.sha256(content).hexdigest(),
                        "import_manifest_key": f"imports/{task_id}/{document_id}/manifest.json",
                    }
                    assert progress["completed_nodes"] == ["entry", "save_file"] and progress["failed_nodes"] == []
                    if index == 3:
                        # 重复准备来源信息验证键稳定，不重复领取已完成的阶段。
                        assert await step_1_prepare_source_file(state) == source
                logger.success("案例通过：{}", description)
        logger.success("node_save_file 自测结束：3/3 通过；未验证真实 MinIO 上传")

    init_logger()
    asyncio.run(self_check())
