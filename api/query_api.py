"""检索 HTTP/SSE 接口和依赖装配；按 session_id 串行，支持停止与历史管理。"""
import asyncio
from dataclasses import dataclass
import json
from urllib.parse import quote
from uuid import uuid4
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.errors import OperationError, error_summary
from common.logging.logger import logger
from processor.query_processor.state import create_query_state
from processor.query_processor.main_graph import build_query_graph
from utils.path_util import PROJECT_ROOT
from utils.query_execution import QueryDependencies, QueryProgress, run_sync
from utils.query_models import QueryLLM, search_web
from utils.query_retrieval import QueryRetrieval
from utils.query_steps import compact_result
from utils.answer_rendering import render_answer
from utils.rate_limit_utils import ApiRateLimiter


class QueryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, str_max_length=512)
    series_name: str = ""
    course_name: str = ""
    project_name: str = ""
    chapter_name: str = ""
    source_file_name: str = ""
    question_bank_name: str = ""
    question_code: str = ""
    question_type: str = ""


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    session_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    request_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    question: str = Field(min_length=1, max_length=2000)
    mode: str = Field(default="answer", pattern=r"^(answer|search)$")
    scope: str = Field(default="all", pattern=r"^(all|course_intro|document_chunk|project_material|question)$")
    top_k: int = Field(default=5, ge=1, le=10)
    use_hyde: bool = False
    use_web: bool = False
    is_stream: bool = True
    filters: QueryFilters = Field(default_factory=QueryFilters)


@dataclass
class QueryRuntime:
    retrieval: object
    history: object
    llm: object
    embed: object
    rerank: object
    web_search: object = search_web


def default_runtime():
    """只创建客户端，BGE/Reranker/LLM 实际调用时再加载。"""
    from common.config.model_config import embedding_config
    from common.config.minio_config import minio_config
    from utils.clients.minio_utils import get_minio_client
    from utils.clients.milvus_utils import get_milvus_client
    from utils.clients.mongo_history_utils import get_history_mongo_tool
    from utils.lm.embedding_utils import generate_embeddings
    from utils.lm.reranker_utils import rerank_documents
    history = get_history_mongo_tool()
    history.ensure_indexes()
    milvus = get_milvus_client()
    if milvus is None:
        raise OperationError("MILVUS_UNAVAILABLE", "Milvus 不可用，请检查检索配置", retryable=True)
    retrieval = QueryRetrieval(milvus, get_minio_client(), minio_config.bucket_name, embedding_config.model_name_or_path)
    return QueryRuntime(retrieval, history, QueryLLM(ApiRateLimiter()), generate_embeddings, rerank_documents)


def sse(event, data):
    if event == "result":
        data = {**data, "answer_html": render_answer(data.get("answer", ""))}
    return "event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n\n"


class QueryService:
    def __init__(self, runtime_factory=None):
        self.factory = runtime_factory or default_runtime
        self._runtime = None
        self._initializing = asyncio.Lock()
        self._sessions = {}
        self._slots = asyncio.Semaphore(4)
        self._jobs = set()
        self._closing = False

    async def runtime(self):
        async with self._initializing:
            if self._runtime is None:
                self._runtime = await run_sync(self.factory)
        return self._runtime

    async def history_tool(self):
        """历史页面只依赖 MongoDB；测试注入时复用提供的历史替身。"""
        if self._runtime is not None or self.factory is not default_runtime:
            return (await self.runtime()).history
        from utils.clients.mongo_history_utils import get_history_mongo_tool
        return await run_sync(get_history_mongo_tool)

    def session_lock(self, session_id):
        return self._sessions.setdefault(session_id, asyncio.Lock())

    async def events(self, payload):
        if self._closing:
            yield sse("error", {"code": "SERVICE_STOPPING", "message": "服务正在关闭"})
            return
        queue = asyncio.Queue(maxsize=128)
        progress = QueryProgress()
        runtime = None

        async def emit(event, data):
            await queue.put((event, data))

        async def execute():
            nonlocal runtime
            try:
                async with self.session_lock(payload["session_id"]), self._slots:
                    runtime = await self.runtime()
                    cached = await run_sync(runtime.history.request_messages, payload["session_id"], payload["request_id"])
                    previous_user = next((m for m in cached if m["role"] == "user"), None)
                    if previous_user and previous_user["text"] != payload["question"]:
                        raise OperationError("REQUEST_ID_CONFLICT", "同一请求编号不能用于不同问题")
                    options = {key: payload[key] for key in ("mode", "scope", "filters", "top_k", "use_hyde", "use_web")}
                    options["is_stream"] = payload.get("is_stream", True)
                    previous_options = previous_user.get("result", {}).get("request_options", options) if previous_user else options
                    previous_options = {**previous_options, "is_stream": previous_options.get("is_stream", True)}
                    if previous_user and previous_options != options:
                        raise OperationError("REQUEST_ID_CONFLICT", "同一请求编号不能更改检索选项")
                    assistant = next((m for m in cached if m["role"] == "assistant" and m["status"] not in {"failed", "interrupted"}), None)
                    if assistant:
                        await emit("result", {**assistant["result"], "replayed": True})
                        return
                    deps = QueryDependencies(runtime.retrieval, runtime.history, runtime.llm, runtime.embed,
                                             runtime.rerank, runtime.web_search, emit, progress)
                    result = await build_query_graph(deps=deps).ainvoke(create_query_state(payload))
                    if result.get("errors"):
                        await emit("error", result["errors"][0])
                    else:
                        await emit("result", {**result["result"], **progress.snapshot()})
            except asyncio.CancelledError:
                if runtime is not None:
                    partial = getattr(progress, "partial_answer", "") or "已停止本次生成。"
                    result = {"session_id": payload["session_id"], "request_id": payload["request_id"],
                              "status": "interrupted", "answer": partial, "references": getattr(progress, "references", [])}
                    try:
                        await run_sync(runtime.history.save_message, payload["session_id"], "assistant", partial,
                            request_id=payload["request_id"], status="interrupted", result=compact_result(result))
                    except Exception:
                        logger.warning("中断的检索历史未能保存：request_id={}", payload["request_id"])
                raise
            except Exception as exc:
                code, message, retryable = error_summary(exc)
                await emit("error", {"code": code, "message": message, "retryable": retryable})
            finally:
                try:
                    queue.put_nowait(("done", {}))
                except asyncio.QueueFull:
                    pass

        job = asyncio.create_task(execute())
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
        try:
            yield sse("start", {"session_id": payload["session_id"], "request_id": payload["request_id"]})
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    if job.done() and queue.empty():
                        break
                    yield sse("ping", {})
                    continue
                yield sse(event, data)
                if event == "done":
                    break
        finally:
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def shutdown(self):
        self._closing = True
        jobs = list(self._jobs)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    async def reference(self, session_id, request_id, reference_id):
        runtime = await self.runtime()
        messages = await run_sync(runtime.history.request_messages, session_id, request_id)
        assistant = next((m for m in messages if m["role"] == "assistant"), {})
        reference = next((r for r in assistant.get("result", {}).get("references", []) if r["id"] == reference_id), None)
        if reference is None or reference["kind"] != "knowledge":
            raise OperationError("REFERENCE_NOT_FOUND", "引用不存在或不属于知识库资料")
        return runtime, await run_sync(runtime.retrieval.restore_reference, reference)


router = APIRouter()


@router.get("/query", include_in_schema=False)
async def query_page():
    return FileResponse(PROJECT_ROOT / "page" / "query.html")


@router.post("/api/query/stream")
async def query_stream(payload: QueryRequest, request: Request):
    return StreamingResponse(request.app.state.query_service.events(payload.model_dump()), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/query/sessions")
async def create_session():
    return {"session_id": uuid4().hex}


@router.get("/api/query/sessions")
async def sessions(request: Request):
    history = await request.app.state.query_service.history_tool()
    return {"sessions": await run_sync(history.list_sessions)}


@router.get("/api/query/sessions/{session_id}/messages")
async def messages(session_id: str, request: Request):
    history = await request.app.state.query_service.history_tool()
    rows = await run_sync(history.recent_messages, session_id, 100)
    return {"messages": [{**{k: row.get(k) for k in ("role", "text", "request_id", "status", "result")},
                          "answer_html": render_answer(row.get("text") or "") if row.get("role") == "assistant" else ""}
                         for row in rows]}


@router.delete("/api/query/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    service = request.app.state.query_service
    async with service.session_lock(session_id):
        history = await service.history_tool()
        return {"deleted": await run_sync(history.clear, session_id)}


@router.get("/api/query/references/{session_id}/{request_id}/{reference_id}")
async def reference_detail(session_id: str, request_id: str, reference_id: str, request: Request):
    _, reference = await request.app.state.query_service.reference(session_id, request_id, reference_id)
    return reference


@router.get("/api/query/references/{session_id}/{request_id}/{reference_id}/file")
@router.get("/api/query/references/{session_id}/{request_id}/{reference_id}/image/{resource_id}")
async def reference_file(session_id: str, request_id: str, reference_id: str, request: Request, resource_id: str = ""):
    runtime, reference = await request.app.state.query_service.reference(session_id, request_id, reference_id)
    row = reference["row"]
    key, media_type = row["source_object_key"], "application/octet-stream"
    headers = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["source_file_name"])}
    if resource_id:
        resource = next((r for r in reference["record"].get("payload", {}).get("resources", []) if r["resource_id"] == resource_id), None)
        if resource is None or resource.get("media_type") not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raise OperationError("REFERENCE_NOT_FOUND", "该引用中没有可显示的图片")
        key, media_type, headers = resource["object_key"], resource["media_type"], {}
    try:
        response = await run_sync(runtime.retrieval.minio.get_object, runtime.retrieval.bucket, key)
    except Exception as exc:
        if getattr(exc, "code", "") in {"NoSuchKey", "NoSuchObject"}:
            raise OperationError("REFERENCE_NOT_FOUND", "来源文件或图片已不存在") from exc
        raise OperationError("SOURCE_UNAVAILABLE", "来源存储服务暂不可用", retryable=True) from exc
    def chunks():
        try:
            while data := response.read(64 * 1024):
                yield data
        finally:
            response.close()
            if hasattr(response, "release_conn"):
                response.release_conn()
    return StreamingResponse(chunks(), media_type=media_type, headers={**headers, "X-Content-Type-Options": "nosniff"})
