"""内容构建器：把解析块与元数据组织成完整 ContentRecord 和检索片段。

纯函数、无 I/O、无客户端；数据库写入、时间戳与拒绝明细由节点负责。
"""

from utils.content_builders.courses import build_course_records
from utils.content_builders.documents import build_document_chunks
from utils.content_builders.projection import (
    MAX_CHUNK_BYTES, RECORD_COLLECTIONS, split_content, stable_chunk_id,
    stable_record_id, to_chunk_drafts,
)
from utils.content_builders.questions import build_question_records

__all__ = [
    "MAX_CHUNK_BYTES", "RECORD_COLLECTIONS", "build_course_records",
    "build_document_chunks", "build_question_records", "split_content",
    "stable_chunk_id", "stable_record_id", "to_chunk_drafts",
]
