"""构建记录：把分块与归属组织成完整 ContentRecord，并投影成三集合检索行。

本节点只做内容组织与字段投影，不生成向量、不写数据库、不发布成功清单；
单条坏数据进入拒绝明细，全部块都建不出记录时明确失败。
"""

import asyncio
from copy import deepcopy

from common.errors import OperationError
from common.logging.logger import logger, node_log, step_log
from processor.import_processor.state import DataDict, ImportState
from utils.content_builders import (
    build_course_records, build_document_chunks, build_question_records,
    split_content, to_chunk_drafts,
)
from utils.content_builders.projection import now_ms
from utils.content_builders.metadata import annotate_blocks
from utils.node_execution import ImportDependencies
from utils.task_utils import add_done_task, add_running_task

_SOURCE_KEYS = ("document_id", "file_name", "object_key", "format", "fingerprint", "import_manifest_key")
_COLLECTIONS = ("courses", "document_chunks", "questions")
_RECORD_TYPES = ("series", "course", "document_chunk", "project_material", "question")
_COLLECTION_REQUIRED = {
    "courses": {"entity_type", "category_path", "audience", "learning_goals",
                "grade_levels", "lesson_count", "study_hours"},
    "document_chunks": {"content_type", "project_id", "project_name", "chapter_id",
                        "chapter_name", "title", "parent_title"},
    "questions": {"question_bank_id", "question_bank_name", "question_code",
                  "question_type", "question_type_name"},
}
_CHUNK_REQUIRED = {
    "chunk_id", "record_id", "document_id", "task_id", "import_manifest_key",
    "payload_object_key", "record_payload", "chunk_index", "content", "knowledge_points",
    "source_file_name", "source_object_key", "source_format", "source_locator",
    "embedding_model", "created_at", "series_id", "series_name", "course_id", "course_name",
}


def _source(state: ImportState) -> DataDict:
    source_file = state.get("source_file")
    if not isinstance(source_file, dict):
        raise OperationError("MISSING_SOURCE_FILE", "构建记录前必须先完成原文件归档")
    return {key: source_file.get(key, "") for key in _SOURCE_KEYS}


def _created_at(state: ImportState, deps: ImportDependencies) -> int:
    """同一任务重试复用任务创建时间；内存任务不可用时退化为当前时间。"""
    try:
        return int(deps.tasks.get(state["task_id"])["created_at"])
    except Exception:
        return now_ms()


@step_log("step_1_group_blocks")
def step_1_group_blocks(state: ImportState) -> DataDict:
    """按内容类型把块聚成构建单元：课程按系列/子课程，题目按题库+题号。"""
    blocks = state.get("parsed_blocks")
    typed = state.get("typed_blocks")
    if not isinstance(blocks, list) or not isinstance(typed, list) or not blocks or not typed:
        raise OperationError("EMPTY_BUILD_INPUT", "没有可构建的解析块或元数据")

    by_id = {block["block_id"]: block for block in blocks}
    groups: DataDict = {"courses": [], "questions": [], "documents": []}
    course_index: dict[tuple, DataDict] = {}
    question_index: dict[tuple, DataDict] = {}
    document_index: dict[str, DataDict] = {}

    for item in typed:
        block = by_id.get(item.get("block_id"))
        if block is None:
            raise OperationError("INVALID_TYPED_BLOCKS", "元数据引用了不存在的解析块")
        content_type = item.get("content_type")
        metadata = item.get("metadata") or {}
        if content_type == "course_intro":
            series = (metadata.get("series_name") or "").strip()
            if not series:
                continue  # 文件标题等无归属块不进入课程构建。
            key = (series, (metadata.get("course_name") or "").strip())
            group = course_index.get(key)
            if group is None:
                group = course_index[key] = {"series_name": series,
                                             "course_name": key[1], "blocks": [], "typed": []}
                groups["courses"].append(group)
            group["blocks"].append(block)
            group["typed"].append(item)
        elif content_type == "question":
            code = (metadata.get("question_code") or "").strip()
            if not code:
                continue  # 题库标题/编码块不是题目正文。
            key = ((metadata.get("question_bank_id") or "").strip(),
                   (metadata.get("question_bank_name") or "").strip(), code)
            group = question_index.get(key)
            if group is None:
                group = question_index[key] = {"bank_id": key[0], "bank_name": key[1],
                                               "question_code": code, "blocks": [], "typed": []}
                groups["questions"].append(group)
            group["blocks"].append(block)
            group["typed"].append(item)
        elif content_type in {"document_chunk", "project_material"}:
            group = document_index.get(content_type)
            if group is None:
                group = document_index[content_type] = {"content_type": content_type,
                                                        "blocks": [], "typed": []}
                groups["documents"].append(group)
            group["blocks"].append(block)
            group["typed"].append(item)

    return groups


@step_log("step_2_build_records")
def step_2_build_records(state: ImportState, groups: DataDict, *,
                               deps: ImportDependencies) -> tuple[list, list[DataDict]]:
    """按内容类型调用构建器，生成完整记录并为每条记录写入创建时间。"""
    source = _source(state)
    created_at = _created_at(state, deps)
    records, rejected = [], []
    blocks, typed = state["parsed_blocks"], state["typed_blocks"]
    # 系列与子课程互相引用，必须按整篇文档一次构建；构建器内部按业务键分组，
    # 单组缺编码/缺题干时记入 rejected 并继续，不中断其他内容。
    if groups["courses"]:
        records.extend(build_course_records(blocks, typed, task_id=state["task_id"],
                                            document_id=state["document_id"],
                                            source=source, rejected=rejected))
    if groups["questions"]:
        records.extend(build_question_records(blocks, typed, task_id=state["task_id"],
                                              document_id=state["document_id"],
                                              source=source, rejected=rejected))
    if groups["documents"]:
        records.extend(build_document_chunks(blocks, typed, task_id=state["task_id"],
                                             document_id=state["document_id"], source=source))

    if not records:
        raise OperationError("EMPTY_BUILD_INPUT", "所有内容块都无法建立有效记录")
    for record in records:
        record["payload"]["created_at"] = created_at
    logger.info("记录构建完成：记录={}，拒绝={}", len(records), len(rejected))
    return records, rejected


@step_log("step_3_build_chunks")
def step_3_build_chunks(state: ImportState, records: list) -> list:
    """把完整记录投影为三集合检索行；超长正文按字节重切，不截断。"""
    chunks, seen = [], set()
    for record in records:
        texts = split_content(record["content"])
        if not texts:
            raise OperationError("EMPTY_RECORD_CONTENT", "存在正文为空的记录，不能投影检索片段")
        for index in range(len(texts)):
            for draft in to_chunk_drafts(record, index, texts=texts):
                fields = draft["fields"]
                missing = (_CHUNK_REQUIRED | _COLLECTION_REQUIRED[draft["collection_key"]]) - set(fields)
                if missing:
                    raise OperationError("INVALID_CHUNK_FIELDS", f"检索行缺少字段：{sorted(missing)}")
                if len(fields["content"].encode("utf-8")) > 65535:
                    raise OperationError("CHUNK_CONTENT_TOO_LONG", "检索片段超过 Milvus 字节上限")
                if draft["collection_key"] == "courses" and fields["entity_type"] not in {"series", "course"}:
                    raise OperationError("INVALID_CHUNK_FIELDS", "课程行的 entity_type 非法")
                if fields["chunk_id"] in seen:
                    raise OperationError("DUPLICATE_CHUNK_ID", "检索片段 ID 重复")
                seen.add(fields["chunk_id"])
                chunks.append(draft)
    return chunks


@step_log("step_4_count")
def step_4_count(state: ImportState, records: list, rejected: list,
                       chunks: list) -> DataDict:
    """汇总记录、拒绝与各集合片段数，保留前序统计。"""
    counts = deepcopy(state["counts"])
    records_by_type = {key: 0 for key in _RECORD_TYPES}
    for record in records:
        if record["content_type"] == "course_intro":
            records_by_type[record["metadata"]["entity_type"]] += 1
        else:
            records_by_type[record["content_type"]] += 1
    chunks_by_collection = {key: 0 for key in _COLLECTIONS}
    for draft in chunks:
        chunks_by_collection[draft["collection_key"]] += 1
    counts.update(records=len(records), rejected_records=len(rejected),
                  records_by_type=records_by_type, chunks_by_collection=chunks_by_collection)
    return counts


@node_log("node_document_split")
async def node_document_split(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    """组织记录构建步骤，全部成功后登记完成并返回增量。"""
    # 1. 添加 document_split 到运行时列表，沿用 md_img 已完成的阶段。
    started_at = await add_running_task(state, "document_split", tasks=deps.tasks)

    # 2. 按前端选择读取明确的课程/题目边界，再调用构建工具。
    state = {**state, "typed_blocks": await asyncio.to_thread(annotate_blocks, state)}

    # 按 typed_blocks 把块组织成记录单元（课程系列/子课程、文档切片、题目）。
    groups = await asyncio.to_thread(step_1_group_blocks, state)

    # 3. 在线程中构建完整记录并切分正文；不占用 HTTP 事件循环。
    records, rejected = await asyncio.to_thread(step_2_build_records, state, groups, deps=deps)

    # 4. 投影为三集合检索行，校验必填、长度与归属一致性。
    chunks = await asyncio.to_thread(step_3_build_chunks, state, records)

    # 5. 汇总统计并校验至少有一条有效记录。
    counts = await asyncio.to_thread(step_4_count, state, records, rejected, chunks)

    # 6. 全部步骤成功后添加 document_split 到已完成列表。
    await add_done_task(state, "document_split", tasks=deps.tasks,
                        started_at=started_at, counts=counts)

    # 7. 返回 records/chunks/rejected_records/counts 增量。
    return {"typed_blocks": state["typed_blocks"], "records": records, "chunks": chunks,
            "rejected_records": rejected, "counts": counts}
