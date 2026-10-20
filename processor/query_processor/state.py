"""单次查询状态；历史持久化交给 MongoDB，会话中不保存向量/客户端。"""
from operator import add
from typing import Annotated, TypedDict


class QueryState(TypedDict, total=False):
    request_id: str
    session_id: str
    question: str
    mode: str
    scope: str
    filters: dict
    top_k: int
    use_hyde: bool
    use_web: bool
    is_stream: bool
    history: list[dict]
    rewritten_query: str
    subject_name: str
    clarification: str
    document_ids: list[str]
    vectors: dict
    vector_hits: list[dict]
    hyde_hits: list[dict]
    web_hits: list[dict]
    candidates: list[dict]
    references: list[dict]
    answer: str
    status: str
    result: dict
    warnings: Annotated[list[dict], add]
    errors: Annotated[list[dict], add]


def create_query_state(payload: dict) -> QueryState:
    return {**payload, "is_stream": payload.get("is_stream", True), "history": [], "warnings": [], "errors": [], "clarification": "",
            "rewritten_query": payload["question"], "subject_name": "", "document_ids": [],
            "vector_hits": [], "hyde_hits": [], "web_hits": [], "references": [],
            "answer": "", "status": "running"}
