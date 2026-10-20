"""内容构建器的文本工具与 Milvus 字段投影；纯函数，不做 I/O。

构建器负责把解析块变成完整 ContentRecord；本模块只负责拆分检索文本、
生成稳定 ID，以及把一条记录投影成目标集合的一行。
"""

import hashlib
import re
from uuid import NAMESPACE_URL, uuid5

from common.schemas.milvus_schema import MAX_INLINE_PAYLOAD_BYTES
from processor.import_processor.state import ContentRecord, DataDict
from common.errors import OperationError
from utils.content_builders.code_blocks import code_ranges

# 普通文本投影的软上限；完整代码允许超过它，数据库硬上限仍为 65535 字节。
MAX_CHUNK_BYTES = 6000
RECORD_COLLECTIONS = {"course_intro": "courses", "document_chunk": "document_chunks",
                      "project_material": "document_chunks", "question": "questions"}
_COMMON_FIELDS = (
    "chunk_id", "record_id", "document_id", "task_id", "import_manifest_key",
    "payload_object_key", "record_payload", "chunk_index", "content", "knowledge_points",
    "source_file_name", "source_object_key", "source_format", "source_locator",
    "embedding_model", "created_at",
)


def plain(text: str) -> str:
    """去掉 Markdown 强调符和列表前缀，供标签识别；不改原始块。"""
    return text.replace("**", "").replace("__", "").lstrip("-*+ \t")


def label(text: str) -> tuple[str | None, str]:
    """拆分“标签: 值”；不是标签行时返回 (None, 去格式文本)。"""
    value = plain(text).strip()
    match = re.match(r"^([^:：\n]{1,16})[:：]\s*(.*)$", value, re.S)
    return (match.group(1).strip(), match.group(2).strip()) if match else (None, value)


def split_items(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，/、]", value) if item.strip()]


def now_ms() -> int:
    from datetime import datetime, timezone

    return int(datetime.now(timezone.utc).timestamp() * 1000)


def stable_record_id(task_id: str, document_id: str, content_type: str, stable_key: str) -> str:
    """任务内重试复用：同一任务/文档/类型/业务键得到同一 record_id。"""
    return str(uuid5(NAMESPACE_URL, f"{task_id}|{document_id}|{content_type}|{stable_key}"))


def stable_chunk_id(record_id: str, chunk_index: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"{record_id}|{chunk_index}"))


def split_content(text: str, *, max_bytes: int = MAX_CHUNK_BYTES) -> list[str]:
    """普通正文按字节分段；代码整体保留，可超软上限但不能超数据库硬上限。"""
    if not isinstance(text, str) or not text:
        return []
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    protected = code_ranges(text)
    if protected:
        chunks, current, cursor = [], "", 0
        for start, end in [*protected, (len(text), len(text))]:
            units = [(part, False) for part in split_content(text[cursor:start], max_bytes=max_bytes)]
            if end > start:
                units.append((text[start:end], True))
            for unit, is_code in units:
                if is_code and len(unit.encode("utf-8")) > 65535:
                    raise OperationError("CODE_BLOCK_TOO_LARGE", "完整代码块超过 Milvus 65535 字节上限，请拆成独立代码示例后重新导入；未截断代码")
                if current and len((current + unit).encode("utf-8")) > max_bytes:
                    chunks.append(current)
                    current = ""
                current += unit
            cursor = end
        if current:
            chunks.append(current)
        return chunks

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        line_size = len(line.encode("utf-8"))
        if line_size > max_bytes:
            if current:
                chunks.append("".join(current))
                current, size = [], 0
            buffer, used = [], 0
            for char in line:
                char_size = len(char.encode("utf-8"))
                if used + char_size > max_bytes:
                    chunks.append("".join(buffer))
                    buffer, used = [], 0
                buffer.append(char)
                used += char_size
            if buffer:
                current, size = buffer, used
            continue
        if size + line_size > max_bytes:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += line_size
    if current:
        chunks.append("".join(current))
    return chunks


def _knowledge_points(record: ContentRecord) -> list[str]:
    points = record["metadata"].get("knowledge_points") or []
    return [str(item)[:256] for item in points if str(item).strip()][:64]


def _course_fields(record: ContentRecord) -> DataDict:
    metadata = record["metadata"]
    return {
        "series_id": str(metadata.get("series_id", ""))[:128],
        "series_name": str(metadata.get("series_name", ""))[:512],
        "course_id": str(metadata.get("course_id", ""))[:128],
        "course_name": str(metadata.get("course_name", ""))[:512],
    }


def to_chunk_drafts(record: ContentRecord, chunk_index: int, *, texts: list[str] | None = None) -> list:
    """把一条完整记录投影成一行目标集合字段；向量与模型标识由 embedding 补。

    texts 由调用方一次性切分后传入，避免每个片段重复切分整段正文。
    """
    chunks = texts if texts is not None else split_content(record["content"])
    if not 0 <= chunk_index < len(chunks):
        raise IndexError("chunk_index 超出检索片段范围")
    text = chunks[chunk_index]
    if len(text.encode("utf-8")) > 65535:
        raise OperationError("CHUNK_CONTENT_TOO_LONG", "检索片段超过 Milvus content 的 65535 字节上限，未截断内容")

    source = record["source"]
    collection = RECORD_COLLECTIONS.get(record["content_type"])
    if collection is None:
        raise ValueError("未知内容类型，无法投影集合字段")
    payload = record["payload"]
    fields = {
        "chunk_id": stable_chunk_id(record["record_id"], chunk_index),
        "record_id": record["record_id"],
        "document_id": record["document_id"],
        "task_id": record["task_id"],
        "import_manifest_key": str(source.get("import_manifest_key", ""))[:2048],
        "payload_object_key": "",
        "record_payload": record,
        "chunk_index": chunk_index,
        "content": text,
        "knowledge_points": _knowledge_points(record),
        "source_file_name": str(source.get("file_name", ""))[:1024],
        "source_object_key": str(source.get("object_key", ""))[:2048],
        "source_format": str(source.get("format", ""))[:16],
        "source_locator": payload.get("source_locator") or {},
        "embedding_model": "",
        "created_at": int(payload["created_at"]),
    }
    fields.update(_course_fields(record))
    if collection == "courses":
        fields.update({
            "entity_type": str(payload.get("entity_type", "course"))[:16],
            "category_path": [str(item)[:256] for item in payload.get("category_path", [])][:16],
            "audience": [str(item)[:128] for item in payload.get("audience", [])][:32],
            "learning_goals": [str(item)[:256] for item in payload.get("learning_goals", [])][:32],
            "grade_levels": [str(item)[:128] for item in payload.get("grade_levels", [])][:32],
            "lesson_count": payload.get("lesson_count"),
            "study_hours": payload.get("study_hours"),
        })
    elif collection == "document_chunks":
        fields.update({
            "content_type": record["content_type"],
            "project_id": str(record["metadata"].get("project_id", ""))[:128],
            "project_name": str(record["metadata"].get("project_name", ""))[:512],
            "chapter_id": str(record["metadata"].get("chapter_id", ""))[:128],
            "chapter_name": str(record["metadata"].get("chapter_name", ""))[:512],
            "title": str(payload.get("title", ""))[:1024],
            "parent_title": str(payload.get("parent_title", ""))[:1024],
        })
    else:
        fields.update({
            "question_bank_id": str(record["metadata"].get("question_bank_id", ""))[:128],
            "question_bank_name": str(record["metadata"].get("question_bank_name", ""))[:512],
            "question_code": str(record["metadata"].get("question_code", ""))[:256],
            "question_type": str(record["metadata"].get("question_type", ""))[:32],
            "question_type_name": str(payload.get("question_type_name", ""))[:128],
        })
    return [{"collection_key": collection, "fields": fields}]


__all__ = [
    "MAX_CHUNK_BYTES", "MAX_INLINE_PAYLOAD_BYTES", "RECORD_COLLECTIONS",
    "label", "now_ms", "plain", "split_content", "split_items",
    "stable_chunk_id", "stable_record_id", "to_chunk_drafts",
]
