"""补充教育主体与一级索引；内容类型始终沿用前端单选。"""
import asyncio
from copy import deepcopy
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from common.logging.logger import node_log, step_log
from processor.import_processor.state import ImportState, DataDict
from utils.content_builders.projection import split_content, to_chunk_drafts
from utils.node_execution import ImportDependencies
from utils.task_utils import add_running_task, add_done_task


@step_log("step_1_prepare_metadata")
def step_1_prepare_metadata(state: ImportState) -> list:
    """优先使用上传归属；原文标签由内容构建工具读取，未知关系不猜测。"""
    records = deepcopy(state["records"])
    upload = state["upload_metadata"]
    for record in records:
        metadata = record["metadata"]
        for key in ("series_id", "series_name", "course_id", "course_name", "project_id", "project_name",
                    "chapter_id", "chapter_name", "question_bank_id", "question_bank_name"):
            if upload.get(key):
                metadata[key] = upload[key]
        metadata["metadata_origin"] = "upload_and_source"
    return records


@step_log("step_2_build_document_index")
def step_2_build_document_index(state: ImportState, records: list) -> list:
    """每文档/主体一条索引；无主体时保留文档自身的可发现性。"""
    source = state["source_file"]
    rows = {}
    for record in records:
        metadata = record["metadata"]
        for kind in ("series", "course", "project", "question_bank"):
            name, identity = metadata.get(f"{kind}_name", ""), metadata.get(f"{kind}_id", "")
            if not name:
                continue
            # 无业务编码时仅生成内部主体标识，不伪造成原资料编码。
            subject_id = identity or str(uuid5(NAMESPACE_URL, f"{kind}:{name}:{metadata.get('series_id', '')}"))
            rows.setdefault((kind, subject_id), (name, metadata))
    if not rows:
        rows[("document", state["document_id"])] = (Path(source["file_name"]).stem, {})
    drafts = []
    for (kind, subject_id), (name, metadata) in rows.items():
        identity = f"{state['task_id']}:{state['document_id']}:{kind}:{subject_id}"
        content = f"{name}\n{source['file_name']}"
        drafts.append({
            "index_id": str(uuid5(NAMESPACE_URL, identity)),
            "task_id": state["task_id"], "document_id": state["document_id"],
            "subject_type": kind, "subject_id": subject_id, "subject_name": name,
            "aliases": [], "index_content": content,
            **{f"{key}_id": metadata.get(f"{key}_id", "") for key in ("series", "course", "project", "question_bank")},
            "source_file_name": source["file_name"], "source_object_key": source["object_key"],
            "import_manifest_key": source["import_manifest_key"], "embedding_model": "",
        })
    return drafts


@step_log("step_3_project_records")
def step_3_project_records(records: list) -> list:
    """补齐归属后重新投影检索文本，完整 payload 与向量字段保持一致。"""
    chunks = []
    for record in records:
        metadata = record["metadata"]
        context = [metadata.get(f"{key}_name", "") for key in ("series", "course", "project", "chapter")]
        text = "\n".join([*dict.fromkeys(x for x in context if x), record["content"]])
        texts = split_content(text)
        for i in range(len(texts)):
            chunks.extend(to_chunk_drafts(record, i, texts=texts))
    return chunks


@node_log("node_metadata_recognition")
async def node_metadata_recognition(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    # 1. 添加节点到运行时列表。
    started_at = await add_running_task(state, "metadata_recognition", tasks=deps.tasks)
    # 2. 整理明确的教育主体与归属，不调用模型判断内容类型。
    records = await asyncio.to_thread(step_1_prepare_metadata, state)
    # 3. 生成主体/文档一级索引草稿。
    indexes = await asyncio.to_thread(step_2_build_document_index, state, records)
    # 4. 在线程中投影带上下文的检索文本，避免大量记录阻塞页面请求。
    chunks = await asyncio.to_thread(step_3_project_records, records)
    counts = deepcopy(state["counts"])
    counts["chunks_by_collection"] = {
        "document_index": len(indexes),
        **{key: sum(c["collection_key"] == key for c in chunks)
           for key in ("courses", "document_chunks", "questions")},
    }
    # 5. 成功后添加到已完成列表并返回增量。
    await add_done_task(state, "metadata_recognition", tasks=deps.tasks, started_at=started_at, counts=counts)
    return {"records": records, "chunks": chunks, "index_drafts": indexes, "counts": counts,
            "document_metadata": {"content_type": state["upload_metadata"]["content_type"],
                                  "subjects": [{"type": r["subject_type"], "name": r["subject_name"]} for r in indexes]}}
