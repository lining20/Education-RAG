"""错误收尾：记录失败原因，清理本任务未发布产物，最后设置任务终态。

补偿节点也显式登记运行与完成，原始业务失败节点保留在失败列表。
"""

import asyncio

from common.logging.logger import logger, node_log, step_log
from processor.import_processor.state import DataDict, ImportState
from utils.clients.milvus_utils import escape_milvus_string
from utils.node_execution import ImportDependencies
from utils.storage_keys import document_prefix
from utils.clients.milvus_collections import collection_names
from utils.task_utils import add_running_task, add_done_task


@step_log("step_1_check_eligibility")
async def step_1_check_eligibility(state: ImportState) -> bool:
    """只有成功领取任务的调用可以清理本任务产物，避免误删并发调用内容。"""
    if state.get("task_claimed") is not True:
        logger.warning("任务未成功领取，跳过产物清理")
        return False
    return True


@step_log("step_2_check_published")
async def step_2_check_published(state: ImportState, *, deps: ImportDependencies) -> bool | None:
    """已写入且 MinIO 成功清单存在时视为已发布；无法核验时返回 None。"""
    persist_result = state.get("persist_result") or {}
    collections = persist_result.get("collections") or {}
    if not any(int(item.get("count", 0)) > 0 for item in collections.values() if isinstance(item, dict)):
        return False
    source_file = state.get("source_file") or {}
    key = source_file.get("import_manifest_key")
    if not key:
        return False
    if (deps.minio_client is None or not isinstance(deps.minio_bucket_name, str)
            or not deps.minio_bucket_name.strip()):
        return None
    try:
        await asyncio.to_thread(deps.minio_client.stat_object,
                                bucket_name=deps.minio_bucket_name, object_name=key)
        return True
    except Exception as exc:
        # 仅明确不存在才视为未发布；网络/权限错误不能当作不存在。
        if getattr(exc, "code", None) in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            return False
        return None


def _collections() -> dict[str, str]:
    return collection_names()


@step_log("step_3_cleanup")
async def step_3_cleanup(state: ImportState, eligible: bool, published: bool | None,
                         *, deps: ImportDependencies) -> DataDict:
    """清理未发布的 Milvus 行与派生 MinIO 对象，保留原文件便于排查。"""
    cleanup: DataDict = {"eligible": eligible, "published": published,
                         "milvus_deleted": {}, "minio_removed": [],
                         "skipped": "", "warnings": []}
    if not eligible:
        cleanup["skipped"] = "CLEANUP_SKIPPED_NOT_CLAIMED"
        return cleanup
    if published is True:
        cleanup["skipped"] = "CLEANUP_SKIPPED_PUBLISHED"
        logger.warning("内容已发布，错误节点不删除已写入内容")
        return cleanup
    if published is None:
        cleanup["skipped"] = "CLEANUP_SKIPPED_NO_STORAGE"
        cleanup["warnings"].append({"code": "CLEANUP_SKIPPED_NO_STORAGE",
                                    "message": "缺少 MinIO 依赖，无法核验发布状态，跳过清理"})
        return cleanup

    task_id = escape_milvus_string(state["task_id"])
    for key, name in _collections().items():
        if deps.milvus_client is None:
            cleanup["warnings"].append({"code": "MISSING_MILVUS_DEPENDENCY",
                                        "message": "缺少 Milvus 客户端，未清理集合"})
            break
        try:
            await asyncio.to_thread(
                deps.milvus_client.delete, collection_name=name,
                filter=f'task_id == "{task_id}"')
            cleanup["milvus_deleted"][key] = True
        except Exception:
            cleanup["milvus_deleted"][key] = False
            cleanup["warnings"].append({"code": "CLEANUP_MILVUS_FAILED",
                                        "message": f"集合 {name} 清理失败，继续处理其他步骤"})

    if deps.minio_client is None or not isinstance(deps.minio_bucket_name, str):
        cleanup["warnings"].append({"code": "MISSING_STORAGE_DEPENDENCY",
                                    "message": "缺少 MinIO 客户端，未清理派生对象"})
        return cleanup

    source_file = state.get("source_file") or {}
    prefix = document_prefix(source_file.get("object_key", ""))
    if not prefix:
        cleanup["warnings"].append({"code": "CLEANUP_SKIPPED_NO_PREFIX",
                                    "message": "缺少来源对象键，未清理派生对象"})
        return cleanup
    prefix = f"{prefix}/"
    keep = source_file.get("object_key") or ""
    try:
        objects = await asyncio.to_thread(
            lambda: list(deps.minio_client.list_objects(
                bucket_name=deps.minio_bucket_name, prefix=prefix, recursive=True)))
        for item in objects:
            name = getattr(item, "object_name", item if isinstance(item, str) else "")
            if not name or name == keep:
                continue
            if name.endswith("/source." + str(source_file.get("format", "")).lower()):
                continue
            try:
                await asyncio.to_thread(deps.minio_client.remove_object,
                                        bucket_name=deps.minio_bucket_name, object_name=name)
                cleanup["minio_removed"].append(name)
            except Exception:
                cleanup["warnings"].append({"code": "CLEANUP_MINIO_FAILED",
                                            "message": "删除派生对象失败，继续处理其他对象"})
    except Exception:
        cleanup["warnings"].append({"code": "CLEANUP_MINIO_FAILED",
                                    "message": "列举派生对象失败，继续收尾"})
    logger.info("失败清理完成：Milvus={}，MinIO 删除={}，警告={}",
                cleanup["milvus_deleted"], len(cleanup["minio_removed"]), len(cleanup["warnings"]))
    return cleanup


@step_log("step_4_assemble_failure")
def step_4_assemble_failure(state: ImportState, cleanup: DataDict) -> DataDict:
    """组织失败结果；保留第一个业务错误，不覆盖为清理错误。"""
    error = state.get("error") or {}
    result = {
        "status": "failed",
        "failed_node": error.get("node", state.get("current_node", "")),
        "code": error.get("code", "UNKNOWN_ERROR"),
        "message": error.get("message", "任务失败，请通过任务标识核对日志"),
        "cleanup": cleanup,
        "warnings": list(state.get("warnings") or []) + list(cleanup.get("warnings", [])),
    }
    return result


@step_log("step_5_set_failed")
async def step_5_set_failed(state: ImportState, result: DataDict, *,
                            deps: ImportDependencies) -> None:
    """设置任务终态；未领取或缺依赖导致更新失败时只记警告。"""
    if state.get("task_claimed") is not True:
        result.setdefault("warnings", []).append({
            "code": "CLEANUP_SKIPPED_NOT_CLAIMED",
            "message": "任务未成功领取，未更新任务终态"})
        return
    try:
        deps.tasks.set_task_result(state["task_id"], state["document_id"], "failed", result)
    except Exception:
        logger.warning("任务失败终态更新失败；首个业务错误与清理结果仍保留在返回值")
        result["state_update_warning"] = "任务终态更新失败，请通过任务标识核对进度"
        result.setdefault("warnings", []).append({
            "code": "TASK_STATE_UPDATE_FAILED",
            "message": "任务终态更新失败，请通过任务标识核对进度"})


@node_log("node_handle_error")
async def node_handle_error(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    """组织错误收尾步骤；不登记运行阶段，不把清理错误覆盖首个业务错误。"""
    # 1. 补偿节点同样登记运行，任务工具允许衔接失败阶段。
    claimed = state.get("task_claimed") is True
    if claimed:
        started_at = await add_running_task(state, "handle_error", tasks=deps.tasks)

    # 2. 校验清理资格：只有成功领取任务的调用可以清理本任务产物。
    eligible = await step_1_check_eligibility(state)

    # 3. 判断内容是否已经发布；已发布的一律不删。
    published = await step_2_check_published(state, deps=deps)

    # 4. 清理未发布产物：Milvus 按 task_id 删除，MinIO 清理派生对象，保留原文件。
    cleanup = await step_3_cleanup(state, eligible, published, deps=deps)

    # 5. 组织失败结果，保留第一个业务错误。
    result = await asyncio.to_thread(step_4_assemble_failure, state, cleanup)

    # 6. 清理执行完成后登记完成；原始失败业务节点仍保留在失败列表。
    if claimed:
        await add_done_task(state, "handle_error", tasks=deps.tasks, started_at=started_at)
    # 7. 设置整个任务失败终态。
    await step_5_set_failed(state, result, deps=deps)

    # 7. 返回 status 与 result 增量。
    return {"status": "failed", "result": result}
