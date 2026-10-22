"""检索 HTTP/SSE 接口和依赖装配；按 session_id 串行，支持停止与历史管理。"""
import asyncio
from dataclasses import dataclass, field
import json
from time import monotonic
from urllib.parse import quote
from uuid import uuid4
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

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


@dataclass
class QueryTask:
    payload: dict
    progress: QueryProgress = field(default_factory=QueryProgress)
    answer: str = ""
    sources: dict = field(default_factory=dict)
    result: dict | None = None
    error: dict | None = None
    finished_at: float | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    job: asyncio.Task | None = None
    started: bool = False
    stop_requested: bool = False

    def notify(self):
        self.changed.set()
        self.changed = asyncio.Event()

    def snapshot(self):
        return {"session_id": self.payload["session_id"], "request_id": self.payload["request_id"],
                "answer": self.answer, **self.sources, **self.progress.snapshot(),
                "status": "finished" if self.finished_at is not None else "running"}


class QueryService:
    def __init__(self, runtime_factory=None):
        self.factory = runtime_factory or default_runtime
        self._runtime = None
        self._initializing = asyncio.Lock()
        self._sessions = {}
        self._slots = asyncio.Semaphore(4)
        self._jobs = set()
        self._tasks = {}
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

    def submit(self, payload):
        """任务归服务持有；提交和重连同一请求不会重复生成。"""
        if self._closing:
            raise OperationError("SERVICE_STOPPING", "服务正在关闭")
        # 已完成内容仍在 Mongo 中；内存仅短期保留重连快照。
        completed = sorted(((key, task) for key, task in self._tasks.items()
                            if task.finished_at is not None), key=lambda item: item[1].finished_at)
        for index, (key, task) in enumerate(completed):
            if monotonic() - task.finished_at > 1800 or index < len(completed) - 100:
                del self._tasks[key]
        key = (payload["session_id"], payload["request_id"])
        if key in self._tasks:
            task = self._tasks[key]
            if task.payload != payload:
                raise OperationError("REQUEST_ID_CONFLICT", "同一请求编号不能更改问题或检索选项")
            return task
        task = QueryTask(payload=dict(payload))
        self._tasks[key] = task
        progress = task.progress
        runtime = None

        async def emit(event, data):
            # 只保留最新状态，无订阅者或慢连接都不会阻塞模型生成。
            if event == "delta":
                task.answer += data["text"]
            elif event == "sources":
                task.sources = data
            elif event == "result":
                task.result = data
                task.answer = data.get("answer", "")
            elif event == "error":
                task.error = data
            task.notify()

        async def execute():
            nonlocal runtime
            task.started = True
            try:
                if task.stop_requested:
                    raise asyncio.CancelledError()
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
                partial = task.answer or "已停止本次生成。"
                result = {"session_id": payload["session_id"], "request_id": payload["request_id"],
                          "status": "interrupted", "answer": partial,
                          "references": getattr(progress, "references", [])}
                if runtime is not None:
                    try:
                        await run_sync(runtime.history.save_message, payload["session_id"], "assistant", partial,
                            request_id=payload["request_id"], status="interrupted", result=compact_result(result))
                    except Exception:
                        logger.warning("中断的检索历史未能保存：request_id={}", payload["request_id"])
                await emit("result", result)
            except Exception as exc:
                code, message, retryable = error_summary(exc)
                await emit("error", {"code": code, "message": message, "retryable": retryable})
            finally:
                task.finished_at = monotonic()
                task.notify()

        job = asyncio.create_task(execute())
        task.job = job
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
        return task

    def get_task(self, session_id, request_id):
        task = self._tasks.get((session_id, request_id))
        if task is None:
            raise OperationError("QUERY_TASK_NOT_FOUND", "任务进度已失效，请重新加载会话历史")
        return task

    def active_requests(self, session_id):
        return [{key: task.payload[key] for key in ("session_id", "request_id", "question", "is_stream")}
                for task in self._tasks.values()
                if task.payload["session_id"] == session_id and task.finished_at is None]

    async def task_events(self, task):
        """订阅者各自等待状态变化；断开订阅不取消任务。"""
        yield sse("start", {key: task.payload[key] for key in ("session_id", "request_id")})
        while True:
            changed = task.changed
            finished = task.finished_at is not None
            yield sse("snapshot", task.snapshot())
            if finished:
                if task.error is not None:
                    yield sse("error", task.error)
                elif task.result is not None:
                    yield sse("result", task.result)
                yield sse("done", {})
                return
            try:
                await asyncio.wait_for(changed.wait(), timeout=15)
            except TimeoutError:
                yield sse("ping", {})

    async def events(self, payload):
        # 兼容旧提交入口，任务生命周期仍独立于此连接。
        async for event in self.task_events(self.submit(payload)):
            yield event

    async def stop(self, session_id, request_id):
        task = self.get_task(session_id, request_id)
        if task.finished_at is None and not task.stop_requested:
            task.stop_requested = True
            if task.started:
                task.job.cancel()
        # 取消请求本身断开也不能再次取消正在保存中断历史的任务。
        await asyncio.shield(task.job)
        return {"stopped": task.result is not None and task.result.get("status") == "interrupted"}

    async def shutdown(self):
        self._closing = True
        await asyncio.gather(*(self.stop(*key) for key, task in self._tasks.items()
                               if task.finished_at is None), return_exceptions=True)

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
    task = request.app.state.query_service.submit(payload.model_dump())
    return StreamingResponse(request.app.state.query_service.task_events(task), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/query/tasks", status_code=202)
async def submit_query(payload: QueryRequest, request: Request):
    task = request.app.state.query_service.submit(payload.model_dump())
    return {key: task.payload[key] for key in ("session_id", "request_id")}


@router.get("/api/query/tasks/{session_id}/{request_id}/events")
async def query_events(session_id: str, request_id: str, request: Request):
    service = request.app.state.query_service
    task = service.get_task(session_id, request_id)
    return StreamingResponse(service.task_events(task), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/query/tasks/{session_id}/{request_id}/stop")
async def stop_query(session_id: str, request_id: str, request: Request):
    return await request.app.state.query_service.stop(session_id, request_id)


@router.post("/api/query/sessions")
async def create_session():
    return {"session_id": uuid4().hex}


@router.get("/api/query/sessions")
async def sessions(request: Request):
    history = await request.app.state.query_service.history_tool()
    return {"sessions": await run_sync(history.list_sessions)}


@router.get("/api/query/sessions/{session_id}/messages")
async def messages(session_id: str, request: Request):
    service = request.app.state.query_service
    active = service.active_requests(session_id)
    history = await service.history_tool()
    rows = await run_sync(history.recent_messages, session_id, 100)
    return {"active_requests": active,
            "messages": [{**{k: row.get(k) for k in ("role", "text", "request_id", "status", "result")},
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
