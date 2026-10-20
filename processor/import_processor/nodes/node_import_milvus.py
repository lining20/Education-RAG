"""四集合入库：完整记录外置、分批写入、回读核对；不删除其他任务资料。"""
import asyncio
import json
from copy import deepcopy
from io import BytesIO

from common.errors import OperationError
from common.logging.logger import node_log, step_log, logger
from common.schemas.milvus_schema import MAX_INLINE_PAYLOAD_BYTES
from processor.import_processor.state import ImportState, DataDict
from utils.clients.milvus_collections import collection_names
from utils.node_execution import ImportDependencies
from utils.import_cancellation import check_import_cancelled
from utils.storage_keys import document_prefix
from utils.task_utils import add_running_task, add_done_task


@step_log("step_1_prepare_rows")
def step_1_prepare_rows(state: ImportState, *, deps: ImportDependencies) -> dict:
    if deps.milvus_client is None:
        raise OperationError("MISSING_MILVUS_DEPENDENCY", "缺少 Milvus 客户端")
    rows = deepcopy(state["embedded_records"])
    if not rows.get("document_index") or not any(rows.get(k) for k in ("courses", "document_chunks", "questions")):
        raise OperationError("EMPTY_PERSIST_INPUT", "必须同时有一级索引与业务内容")
    prefix = document_prefix(state["source_file"]["object_key"])
    externalized = {}
    for key in ("courses", "document_chunks", "questions"):
        for row in rows[key]:
            check_import_cancelled(deps.stop_event)
            record_id = row["record_id"]
            blob = json.dumps(row["record_payload"], ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(blob) <= MAX_INLINE_PAYLOAD_BYTES:
                continue
            if record_id not in externalized:
                object_key = f"{prefix}/payloads/{record_id}.json"
                deps.minio_client.put_object(
                    bucket_name=deps.minio_bucket_name, object_name=object_key,
                    data=BytesIO(blob), length=len(blob), content_type="application/json")
                externalized[record_id] = object_key
            row["record_payload"] = {}
            row["payload_object_key"] = externalized[record_id]
    return {"rows": rows, "payload_objects": list(externalized.values())}


@step_log("step_2_upsert_and_verify")
async def step_2_upsert_and_verify(state: ImportState, prepared: dict, *, deps: ImportDependencies) -> dict:
    result = {"collections": {}, "payload_objects": prepared["payload_objects"], "embedding_model": ""}
    records = set()
    for key, name in collection_names().items():
        rows = prepared["rows"][key]
        primary = "index_id" if key == "document_index" else "chunk_id"
        ids = []
        try:
            for start in range(0, len(rows), 128):
                check_import_cancelled(deps.stop_event)
                batch = rows[start:start + 128]
                response = await asyncio.to_thread(deps.milvus_client.upsert, collection_name=name, data=batch)
                if response.get("upsert_count") != len(batch):
                    raise OperationError("MILVUS_WRITE_MISMATCH", "实写数量与提交数量不一致")
                batch_ids = [row[primary] for row in batch]
                stored = await asyncio.to_thread(deps.milvus_client.get, collection_name=name,
                    ids=batch_ids, output_fields=[primary, "task_id"], consistency_level="Strong")
                actual = {row[primary] for row in stored if row.get("task_id") == state["task_id"]}
                if actual != set(batch_ids):
                    raise OperationError("MILVUS_WRITE_MISMATCH", "回读未找到本任务的全部数据，不能发布")
                ids.extend(batch_ids)
            result["collections"][name] = {"count": len(ids), "primary_key": primary, "ids": ids}
            if key != "document_index":
                records.update(row["record_id"] for row in rows)
            if rows:
                result["embedding_model"] = rows[0]["embedding_model"]
            logger.info("集合写入并核验完成：{}，行数={}", name, len(ids))
        except OperationError:
            raise
        except Exception as exc:
            raise OperationError("MILVUS_UPSERT_FAILED", f"集合 {name} 写入或回读失败", retryable=True) from exc
    result["records"] = len(records)
    return result


@node_log("node_import_milvus")
async def node_import_milvus(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    # 1. 添加节点到运行时列表。
    started_at = await add_running_task(state, "import_milvus", tasks=deps.tasks)
    # 2. 在线程中复制和序列化完整记录；超预算的 JSON 写入 MinIO。
    prepared = await asyncio.to_thread(step_1_prepare_rows, state, deps=deps)
    # 3. 旧内容已在 save_file 清理；分批写四集合并按主键回读核验。
    result = await step_2_upsert_and_verify(state, prepared, deps=deps)
    counts = deepcopy(state["counts"])
    counts["persisted_records"] = result["records"]
    counts["persisted_by_collection"] = {
        key: result["collections"][name]["count"] for key, name in collection_names().items()}
    # 4. 全部写入成功后登记完成，留给 finish 最后发布。
    await add_done_task(state, "import_milvus", tasks=deps.tasks, started_at=started_at, counts=counts)
    return {"persist_result": result, "counts": counts}
