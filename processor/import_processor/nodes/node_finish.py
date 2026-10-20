"""发布：校验四集合实写结果，写入 MinIO 成功清单，最后对齐任务终态。

成功清单是唯一发布提交点；清单写成功后才更新内存任务状态，内存更新失败
只记警告，不删除或回滚已发布内容。
"""

import asyncio
import json
from copy import deepcopy
from io import BytesIO

from common.errors import OperationError
from common.logging.logger import logger, node_log, step_log
from processor.import_processor.state import DataDict, ImportState
from utils.content_builders.projection import now_ms
from utils.node_execution import ImportDependencies
from utils.task_utils import add_done_task, add_running_task


@step_log("step_1_validate_publish")
def step_1_validate_publish(state: ImportState) -> DataDict:
    """校验发布条件：有实写行、记录数一致、没有首个业务错误。"""
    if state.get("error") is not None:
        raise OperationError("PUBLISH_VALIDATION_FAILED", "存在业务错误，不能发布成功清单")
    persist_result = state.get("persist_result")
    if not isinstance(persist_result, dict) or not isinstance(persist_result.get("collections"), dict):
        raise OperationError("PUBLISH_VALIDATION_FAILED", "缺少入库实写明细，不能发布")

    collections = persist_result["collections"]
    total_rows = sum(int(item.get("count", 0)) for item in collections.values())
    if total_rows <= 0:
        raise OperationError("PUBLISH_VALIDATION_FAILED", "四集合实际写入为 0，不能发布成功清单")
    records = state.get("records")
    persisted_records = int(state["counts"].get("persisted_records", 0))
    if not isinstance(records, list) or persisted_records != len(records):
        raise OperationError("PUBLISH_VALIDATION_FAILED", "实写记录数与构建记录数不一致")
    source_file = state.get("source_file")
    if not isinstance(source_file, dict) or not source_file.get("import_manifest_key"):
        raise OperationError("PUBLISH_VALIDATION_FAILED", "缺少来源信息或成功清单键")

    logger.info("发布校验通过：记录={}，行数={}，集合={}",
                persisted_records, total_rows, len(collections))
    return {"total_rows": total_rows, "records": persisted_records,
            "collections": deepcopy(collections),
            "embedding_model": persist_result.get("embedding_model", "")}


@step_log("step_2_write_manifest")
def step_2_write_manifest(state: ImportState, summary: DataDict, *,
                                deps: ImportDependencies) -> DataDict:
    """把成功清单写入来源文件预留的对象键；失败不得把任务标记成功。"""
    if (deps.minio_client is None or not isinstance(deps.minio_bucket_name, str)
            or not deps.minio_bucket_name.strip()):
        raise OperationError("MISSING_STORAGE_DEPENDENCY", "发布成功清单需要 MinIO 客户端和桶名")

    source_file = state["source_file"]
    manifest = {
        "task_id": state["task_id"], "document_id": state["document_id"],
        "status": "succeeded", "published_at": now_ms(),
        "collections": {
            name: deepcopy(item)
            for name, item in summary["collections"].items()
        },
        "embedding_model": summary["embedding_model"],
        "source": {
            "file_name": source_file.get("file_name", ""),
            "object_key": source_file.get("object_key", ""),
            "fingerprint": source_file.get("fingerprint", ""),
            "format": source_file.get("format", ""),
        },
        "counts": deepcopy(state["counts"]),
    }
    key = source_file["import_manifest_key"]
    blob = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    try:
        deps.minio_client.put_object(
            bucket_name=deps.minio_bucket_name, object_name=key,
            data=BytesIO(blob), length=len(blob), content_type="application/json",
        )
    except Exception as exc:
        raise OperationError("MANIFEST_WRITE_FAILED", "成功清单写入 MinIO 失败，任务不能标记成功",
                             retryable=True) from exc
    logger.info("成功清单已写入：key={} 字节={}", key, len(blob))
    return {"key": key, "payload": manifest}


@step_log("step_3_assemble_result")
def step_3_assemble_result(state: ImportState, summary: DataDict, manifest: DataDict) -> DataDict:
    """组织最终结果：数量、警告、清单键与集合明细。"""
    return {
        "status": "succeeded",
        "task_id": state["task_id"], "document_id": state["document_id"],
        "manifest_key": manifest["key"],
        "records": summary["records"], "total_rows": summary["total_rows"],
        "collections": {name: item.get("count", 0) for name, item in summary["collections"].items()},
        "embedding_model": summary["embedding_model"],
        "counts": deepcopy(state["counts"]),
        "warnings": deepcopy(state.get("warnings") or []),
        "rejected_records": deepcopy(state.get("rejected_records") or []),
    }


@step_log("step_4_set_task_status")
async def step_4_set_task_status(state: ImportState, result: DataDict, *,
                                 deps: ImportDependencies) -> None:
    """清单已发布后对齐内存任务；失败只记警告，不回滚已发布内容。"""
    try:
        deps.tasks.set_task_result(state["task_id"], state["document_id"], "succeeded", result)
    except Exception:
        logger.warning("成功清单已发布，但内存任务状态更新失败；不回滚已发布内容")
        result["memory_update_warning"] = "任务已发布，但内存状态更新失败"


@node_log("node_finish")
async def node_finish(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    """组织发布步骤，清单写成功后才更新任务终态。"""
    # 1. 添加 finish 到运行时列表，沿用 import_milvus 已完成的阶段。
    started_at = await add_running_task(state, "finish", tasks=deps.tasks)

    # 2. 校验发布条件：至少一条有效记录，四集合实写数量与记录数一致。
    summary = await asyncio.to_thread(step_1_validate_publish, state)

    # 3. 写入 MinIO 成功清单，这是唯一发布提交点。
    manifest = await asyncio.to_thread(step_2_write_manifest, state, summary, deps=deps)

    # 4. 组织最终结果（数量、警告、清单键）。
    result = await asyncio.to_thread(step_3_assemble_result, state, summary, manifest)

    # 5. 添加 finish 到已完成列表；set_task_result 要求该阶段为 completed，
    #    因此必须先登记完成，再对齐任务终态。
    await add_done_task(state, "finish", tasks=deps.tasks,
                        started_at=started_at, counts=state["counts"])

    # 6. 对齐任务终态；清单已发布，内存更新失败只告警不回滚。
    await step_4_set_task_status(state, result, deps=deps)

    # 7. 返回 status 与 result 增量。
    return {"status": "succeeded", "result": result}

