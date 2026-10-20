"""导入 HTTP 接口、依赖装配和批次调度；前后端交互集中在本文件。

启动：uv run uvicorn api.main:app --host 0.0.0.0 --port 8200 --workers 1
单进程内存任务；API 不加载模型，BGE-M3 在真正向量化时按需加载。
"""
import asyncio
from contextlib import asynccontextmanager, ExitStack
from dataclasses import replace
from pathlib import Path, PureWindowsPath
import shutil
from threading import Event
from uuid import uuid4, uuid5, NAMESPACE_URL
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from common.errors import OperationError, error_summary
from common.logging.logger import init_logger, logger
from processor.import_processor.main_graph import build_import_graph
from processor.import_processor.state import create_initial_state
from utils.node_execution import ImportDependencies, bind_node
from utils.parsers.common import ParseSession, ResourceContext
from utils.path_util import PROJECT_ROOT
from utils.task_utils import ImportTaskStore
from utils.zip_preprocessor import prepare_zip_package, ZipLimits
from utils.rate_limit_utils import ApiRateLimiter

CONTENT_TYPES = {"course_intro", "document_chunk", "project_material", "question"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def default_dependencies(tasks: ImportTaskStore) -> ImportDependencies:
    """在后台线程中准备共享客户端；缺桶/集合自动创建，已存在不覆盖。"""
    from common.config.mineru_config import mineru_config
    from common.config.lm_config import lm_config
    from common.config.minio_config import minio_config
    from utils.clients.minio_utils import get_minio_client
    from utils.clients.milvus_utils import get_milvus_client
    from utils.clients.milvus_collections import ensure_collections
    from utils.parsers.registry import build_document_parsers
    minio = get_minio_client()
    milvus = get_milvus_client()
    if milvus is None:
        raise OperationError("MILVUS_UNAVAILABLE", "Milvus 不可用，请检查配置和服务", retryable=True)
    ensure_collections(milvus)
    return ImportDependencies(tasks=tasks, max_file_bytes=MAX_UPLOAD_BYTES,
        minio_client=minio, minio_bucket_name=minio_config.bucket_name,
        milvus_client=milvus, document_parsers=build_document_parsers(mineru_config=mineru_config),
        image_understanding_enabled=lm_config.image_understanding_enabled)


class ImportService:
    """持有批次临时目录与任务；每文档独立 State、图和解析会话。"""
    def __init__(self, *, tasks=None, dependencies_factory=None, upload_root=None,
                 max_upload_bytes=MAX_UPLOAD_BYTES, concurrency=8, parse_concurrency=4,
                 image_concurrency=8, image_rate_limiter=None):
        if concurrency < 1 or parse_concurrency < 1 or image_concurrency < 1:
            raise ValueError("文档和图片并发数量必须大于 0")
        self.tasks = tasks if tasks is not None else ImportTaskStore()
        self.dependencies_factory = dependencies_factory or default_dependencies
        self.upload_root = Path(upload_root or PROJECT_ROOT / ".uploads")
        self.max_upload_bytes = max_upload_bytes
        self._semaphore = asyncio.Semaphore(concurrency)
        # 在途文档与解析/图片分别限并发，图片等待不占用解析名额。
        self._parse_semaphore = asyncio.Semaphore(parse_concurrency)
        self._image_semaphore = asyncio.Semaphore(image_concurrency)
        self._image_concurrency = image_concurrency
        from common.config.lm_config import lm_config
        self._image_rate_limiter = image_rate_limiter if image_rate_limiter is not None else ApiRateLimiter(
            lm_config.vl_max_requests, lm_config.vl_rate_window_seconds)
        self._dependency_lock = asyncio.Lock()
        self._file_locks = {}
        self._batches = {}
        self._jobs = {}
        self._closing = False
        self._stop_event = Event()

    async def submit(self, uploads: list[UploadFile], metadata: dict) -> dict:
        if self._closing:
            raise OperationError("SERVICE_STOPPING", "服务正在关闭，请稍后重试", retryable=True)
        if metadata.get("content_type") not in CONTENT_TYPES:
            raise OperationError("INVALID_UPLOAD_METADATA", "必须选择内容类型")
        if not uploads:
            raise OperationError("EMPTY_UPLOAD_FILE", "请选择至少一个文件")
        batch_id = uuid4().hex
        directory = (self.upload_root / batch_id).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        stack = ExitStack()
        try:
            documents = []
            warnings = []
            # 全部文件准备完成后才创建任务，避免后面的无效文件触发部分覆盖。
            for index, upload in enumerate(uploads):
                filename = PureWindowsPath((upload.filename or "").replace("/", "\\")).name
                suffix = Path(filename).suffix.lower()
                if suffix not in {".md", ".pdf", ".docx", ".zip"}:
                    raise OperationError("UNSUPPORTED_FILE_FORMAT", f"{filename}：仅支持 MD、PDF、DOCX 或 ZIP；旧 DOC 请先转换")
                if len(filename.encode("utf-8")) > 1024:
                    raise OperationError("INVALID_UPLOAD_METADATA", "文件名过长")
                # 每个上传项使用独立目录，多个 ZIP 的解压和图片引用互不干扰。
                folder = directory / str(index)
                folder.mkdir()
                path = folder / f"upload{suffix}"
                size = 0
                with path.open("wb") as target:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > self.max_upload_bytes:
                            raise OperationError("FILE_TOO_LARGE", f"{filename}：上传文件超过大小限制")
                        # 上传中断时先等本次写盘结束，再关闭文件及清理目录。
                        writing = asyncio.create_task(asyncio.to_thread(target.write, chunk))
                        try:
                            await asyncio.shield(writing)
                        except asyncio.CancelledError:
                            await writing
                            raise
                if not size:
                    raise OperationError("EMPTY_UPLOAD_FILE", f"{filename}：上传文件为空")
                if suffix == ".zip":
                    # 线程调用取消时也等解压结束，再释放整个批次的上下文。
                    preparation = asyncio.create_task(asyncio.to_thread(
                        stack.enter_context, prepare_zip_package(path, limits=ZipLimits(max_file_bytes=self.max_upload_bytes))))
                    try:
                        package = await asyncio.shield(preparation)
                    except asyncio.CancelledError:
                        await preparation
                        raise
                    warnings.extend(package.warnings)
                    for doc in package.documents:
                        context = ResourceContext(package.resource_root, doc.relative_path,
                            lambda ref, doc=doc, package=package: package.resolve_resource(doc, ref))
                        relative_path = f"{filename}/{doc.relative_path}" if len(uploads) > 1 else doc.relative_path
                        documents.append((Path(doc.file_path), Path(doc.relative_path).name, relative_path, context))
                else:
                    documents.append((path, filename, filename, None))
            names = [doc[1] for doc in documents]
            if len(set(names)) != len(names):
                raise OperationError("DUPLICATE_FILE_NAME", "本批次存在同名文档（包括 ZIP 内文档），请先改名；替换规则以文件名为准")
            items, states = [], []
            for document_path, display_name, relative_path, context in documents:
                task_id = uuid4().hex
                document_id = str(uuid5(NAMESPACE_URL, f"source-file:{display_name}"))
                self.tasks.create_pending(task_id, document_id)
                state = create_initial_state(task_id=task_id, document_id=document_id,
                    file_path=str(document_path), upload_metadata={**metadata, "source_file_name": display_name})
                state["warnings"] = list(warnings)
                states.append((state, context))
                items.append({"task_id": task_id, "document_id": document_id, "file_name": display_name,
                              "relative_path": relative_path})
            self._batches[batch_id] = {"batch_id": batch_id, "tasks": items, "warnings": warnings}
            job = asyncio.create_task(self._run_batch(batch_id, states, stack, directory))
            self._jobs[batch_id] = job
            job.add_done_callback(lambda task: self._jobs.pop(batch_id, None))
            logger.info("导入批次已创建：batch_id={} 文档数={} 类型={}", batch_id, len(items), metadata["content_type"])
            return {"batch_id": batch_id, "tasks": items, "status": "pending", "warnings": warnings}
        except BaseException:
            await asyncio.to_thread(stack.close)
            await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)
            raise

    async def _run_batch(self, batch_id, states, stack, directory):
        try:
            # 防止多个批次同时首次建桶/建集合。
            async with self._dependency_lock:
                from utils.import_cancellation import check_import_cancelled
                check_import_cancelled(self._stop_event)
                base = await asyncio.to_thread(self.dependencies_factory, self.tasks)
            outcomes = await asyncio.gather(*(self._run_document(state, context, base) for state, context in states), return_exceptions=True)
            for (state, _), outcome in zip(states, outcomes):
                if isinstance(outcome, BaseException):
                    self.tasks.abort_task(state["task_id"], state["document_id"], {
                        "node": "handle_error", "code": "IMPORT_EXECUTION_FAILED",
                        "message": "任务收尾异常，请核对日志和未发布产物", "retryable": True})
        except Exception as exc:
            code, message, retryable = error_summary(exc)
            logger.error("导入批次初始化失败：batch_id={} code={}", batch_id, code)
            for state, _ in states:
                self.tasks.reject_pending(state["task_id"], state["document_id"],
                    {"node": "entry", "code": code, "message": message, "retryable": retryable})
        finally:
            # gather 的全部文档已结束，才释放共享图片目录。
            await asyncio.to_thread(stack.close)
            await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)

    async def _run_document(self, initial, context, base):
        # 同名锁覆盖整个图及失败收尾；同名任务不能交叉删除对方新产物。
        name = initial["upload_metadata"]["source_file_name"]
        lock = self._file_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if self._closing:
                self.tasks.reject_pending(initial["task_id"], initial["document_id"],
                    {"node": "entry", "code": "IMPORT_CANCELLED", "message": "服务关闭，排队任务已停止", "retryable": True})
                return
            await self._run_locked_document(initial, context, base)

    async def _run_locked_document(self, initial, context, base):
        state = dict(initial)
        session = ParseSession()
        deps = replace(base, tasks=self.tasks, resource_context=context, parse_session=session,
                       max_file_bytes=self.max_upload_bytes, stop_event=self._stop_event,
                       image_semaphore=self._image_semaphore, image_rate_limiter=self._image_rate_limiter,
                       image_concurrency=self._image_concurrency, parse_semaphore=self._parse_semaphore)
        try:
            async with self._semaphore:
                from utils.import_cancellation import check_import_cancelled
                check_import_cancelled(self._stop_event)
                graph = build_import_graph(deps=deps)
                # 保存每步增量，意外退出时仍能按本任务的来源和清单安全收尾。
                async for updates in graph.astream(state, stream_mode="updates"):
                    for update in updates.values():
                        state.update(update)
                if self.tasks.get(state["task_id"])["status"] not in {"succeeded", "failed"}:
                    raise OperationError("IMPORT_INCOMPLETE", "流程未正常结束，请核对错误收尾日志")
        except (Exception, asyncio.CancelledError) as exc:
            from processor.import_processor.nodes.node_handle_error import node_handle_error
            code, message, retryable = ("IMPORT_CANCELLED", "服务关闭，导入被中断", True) if isinstance(exc, asyncio.CancelledError) else error_summary(exc)
            error = state.get("error") or {"node": state.get("current_node") or "entry", "code": code,
                                           "message": message, "retryable": retryable}
            progress = self.tasks.get(state["task_id"])
            if progress["status"] == "pending":
                self.tasks.reject_pending(state["task_id"], state["document_id"], error)
            elif progress["status"] == "running":
                current = progress["current_node"]
                if progress["node_progress"][current]["status"] == "running":
                    await self.tasks.fail_node(state["task_id"], state["document_id"], current, error)
                state.update(task_claimed=True, current_node=current, error=error, status="failed")
                await bind_node("handle_error", node_handle_error, deps)(state)
            logger.warning("导入文档收尾：task_id={} code={}", state["task_id"], code)
        finally:
            await asyncio.to_thread(session.close)

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def get_batch(self, batch_id):
        batch = self._batches.get(batch_id)
        if batch is None:
            raise OperationError("TASK_NOT_FOUND", "批次不存在，服务重启后请重新上传")
        tasks = [{**item, **self.get_task(item["task_id"])} for item in batch["tasks"]]
        finished = all(task["status"] in {"succeeded", "failed"} for task in tasks)
        status = ("failed" if any(t["status"] == "failed" for t in tasks) else "succeeded") if finished else "running"
        return {**batch, "tasks": tasks, "status": status,
                "completed": sum(t["status"] == "succeeded" for t in tasks),
                "failed": sum(t["status"] == "failed" for t in tasks), "total": len(tasks)}

    async def shutdown(self):
        self._closing = True
        self._stop_event.set()
        jobs = list(self._jobs.values())
        # 只等待当前同步操作退出及失败收尾，不继续整批文档或后续图片。
        if jobs:
            logger.info("服务关闭：停止 {} 个导入批次，取消视觉请求并清理临时资源", len(jobs))
            await asyncio.gather(*jobs, return_exceptions=True)


def create_app(*, service=None, query_service=None):
    from api.query_api import QueryService, router as query_router
    from api.auth_api import router as auth_router
    from utils.clients.upload_auth import UploadAuth, COOKIE_NAME
    @asynccontextmanager
    async def lifespan(app):
        init_logger()
        yield
        await asyncio.gather(app.state.query_service.shutdown(), app.state.import_service.shutdown())
        await asyncio.to_thread(app.state.upload_auth.close)

    app = FastAPI(title="教育智库", lifespan=lifespan)
    app.state.import_service = service if service is not None else ImportService()
    app.state.query_service = query_service if query_service is not None else QueryService()
    app.state.upload_auth = UploadAuth()
    app.include_router(query_router)
    app.include_router(auth_router)
    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "page" / "assets"), name="static")

    @app.middleware("http")
    async def protect_upload(request, call_next):
        path = request.url.path
        protected = path.rstrip("/") in {"/import", "/ui"} or path.startswith("/api/import/")
        # 登录、退出和上传的浏览器请求只接受同源来源。
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (protected or path.startswith("/api/auth/")):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse(status_code=403, content={"message": "不允许跨站操作"})
        if protected:
            try:
                user = await asyncio.to_thread(app.state.upload_auth.current_user, request.cookies.get(COOKIE_NAME))
            except Exception as exc:
                code, message, _ = error_summary(exc)
                return JSONResponse(status_code=503, content={"code": code, "message": message})
            if not user:
                if path.startswith("/api/"):
                    return JSONResponse(status_code=401, content={"code": "LOGIN_REQUIRED", "message": "请登录后上传资料"})
                return RedirectResponse("/login", status_code=303)
        response = await call_next(request)
        if protected or path.startswith("/api/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(OperationError)
    async def operation_error(request, exc):
        status = 401 if exc.code == "INVALID_CREDENTIALS" else 429 if exc.code == "LOGIN_RATE_LIMITED" else 404 if exc.code in {"TASK_NOT_FOUND", "REFERENCE_NOT_FOUND", "REFERENCE_EXPIRED"} else 413 if exc.code == "FILE_TOO_LARGE" else 503 if exc.retryable else 400
        return JSONResponse(status_code=status, content={"code": exc.code, "message": exc.message})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        message = "请填写有效的用户名和密码" if request.url.path.startswith("/api/auth") else "请检查问题、筛选条件或会话编号" if request.url.path.startswith("/api/query") else "请选择文件和内容类型"
        return JSONResponse(status_code=422, content={"code": "VALIDATION_ERROR", "message": message})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logger.error("导入接口异常：type={}", type(exc).__name__)
        return JSONResponse(status_code=500, content={"code": "INTERNAL_ERROR", "message": "服务内部错误，请查看日志"})

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(PROJECT_ROOT / "page" / "query.html")

    @app.get("/import", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    async def index():
        return FileResponse(PROJECT_ROOT / "page" / "index.html")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "note": "仅表示 HTTP 服务运行；存储和模型在导入时检查"}

    @app.post("/api/import/tasks", status_code=202)
    async def submit(request: Request, file: list[UploadFile] = File(...), content_type: str = Form(...),
                     series_name: str = Form(""), course_name: str = Form(""), project_name: str = Form(""),
                     chapter_name: str = Form(""), question_bank_name: str = Form("")):
        metadata = {"content_type": content_type, "series_name": series_name.strip(),
                    "course_name": course_name.strip(), "project_name": project_name.strip(),
                    "chapter_name": chapter_name.strip(), "question_bank_name": question_bank_name.strip()}
        try:
            return await request.app.state.import_service.submit(file, metadata)
        finally:
            for upload in file:
                await upload.close()

    @app.get("/api/import/tasks/{task_id}")
    async def task(task_id: str, request: Request):
        return request.app.state.import_service.get_task(task_id)

    @app.get("/api/import/batches/{batch_id}")
    async def batch(batch_id: str, request: Request):
        return request.app.state.import_service.get_batch(batch_id)

    return app


app = create_app()
