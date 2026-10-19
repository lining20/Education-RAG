"""共享日志：同步/异步装饰器、任务上下文、单调计时与安全异常栈。

导入不创建日志文件或移除 sink；应用入口显式调用 init_logger()，可重复调用。
节点边界记录一次栈，步骤只记录摘要；装饰器重新抛错，不负责图路由。
"""
from collections.abc import Mapping
from functools import wraps
import inspect
import sys
from threading import Lock
from time import perf_counter
import traceback
from typing import Any, Callable
from loguru import logger as _logger
from common.config._env import env_bool, env_text
from common.errors import error_summary
from utils.path_util import PROJECT_ROOT

LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE_PATH = LOG_DIR / "app_{time:YYYYMMDD}.log"
LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
_init_lock = Lock()
_initialized = globals().get("_initialized", False)
_sink_ids = globals().get("_sink_ids", [])


def fix_log_position(record: dict[str, Any]) -> None:
    """补齐上下文字段，跳过日志模块自身和 Loguru 栈帧。"""
    for key in ("flow", "task_id", "document_id", "request_id", "session_id", "conversation_id",
                "node", "step", "status", "duration_ms"):
        record["extra"].setdefault(key, "-")
    frame = inspect.currentframe()
    try:
        while frame is not None:
            module = frame.f_globals.get("__name__", "")
            if module != __name__ and not module.startswith("loguru"):
                record.update(name=module, function=frame.f_code.co_name, line=frame.f_lineno)
                break
            frame = frame.f_back
    finally:
        del frame


logger = _logger.patch(fix_log_position)


def format_log_record(record: dict[str, Any]) -> str:
    """处理器也接收第三方原生 Loguru；不假设它经过项目 patch。"""
    for key in ("flow", "task_id", "document_id", "request_id", "session_id", "conversation_id",
                "node", "step", "status", "duration_ms"):
        record["extra"].setdefault(key, "-")
    return LOG_FORMAT + "\n{exception}"


def init_logger():
    """显式且幂等地配置输出；复用统一环境解析，保留其他自定义 sink。"""
    global _initialized
    with _init_lock:
        if _initialized:
            return logger
        console = env_bool("LOG_CONSOLE_ENABLE", True)
        file_enabled = env_bool("LOG_FILE_ENABLE", True)
        options = {"format": format_log_record, "backtrace": False, "diagnose": False}
        try:
            _logger.remove(0)  # 只替换 Loguru 自带的默认输出。
        except ValueError:
            pass
        if console:
            _sink_ids.append(logger.add(
                sys.stdout, level=env_text("LOG_CONSOLE_LEVEL", "INFO"), colorize=True,
                # 可仅关闭某段代码的控制台输出；内存/文件 sink 仍保留完整记录。
                filter=lambda record: record["extra"].get("console_output", True) is not False,
                **options,
            ))
        if file_enabled:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _sink_ids.append(logger.add(
                LOG_FILE_PATH, level=env_text("LOG_FILE_LEVEL", "INFO"),
                encoding="utf-8", colorize=False, rotation="00:00",
                retention=env_text("LOG_FILE_RETENTION", "7 days"), **options,
            ))
        _initialized = True
    return logger


def trace_fields(value: Any) -> dict[str, str]:
    """提取可安全记录的标识，供日志装饰器与统一执行边界共用。"""
    if not isinstance(value, Mapping):
        return {}
    fields = {}
    for key in ("task_id", "document_id", "request_id", "session_id", "conversation_id"):
        item = value.get(key)
        if isinstance(item, str) and item and len(item) <= 64 and item.isprintable():
            fields[key] = item
    return fields


def _failure_log(kind: str, name: str, exc: BaseException, started: float, trace_id: str) -> None:
    elapsed = round((perf_counter() - started) * 1000, 3)
    label = "节点" if kind == "node" else "步骤"
    trace = f"，追踪ID={trace_id}" if kind == "node" else ""
    if not isinstance(exc, Exception):
        logger.bind(status="interrupted", duration_ms=elapsed).warning("[{}] {}被中断{}", name, label, trace)
        return
    code, message, _ = error_summary(exc)
    # 保存原因链的栈帧位置，不展开局部变量/SDK 原始消息，避免 URI、密钥或正文泄漏。
    stacks = []
    current = exc
    visited = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        stacks.append(type(current).__name__ + "\n" + "".join(traceback.format_tb(current.__traceback__)))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    detail = "\n".join(stacks) if kind == "node" else type(exc).__name__
    logger.bind(status="failed", duration_ms=elapsed).error(
        "[{}] {}失败{}，code={} message={}\n{}", name, label, trace, code, message, detail,
    )


def _execution_log(kind: str, name: str) -> Callable:
    label = "节点" if kind == "node" else "步骤"

    def decorate(func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                fields = trace_fields(args[0] if args else kwargs.get("state"))
                with logger.contextualize(**fields, **{kind: name}):
                    started = perf_counter()
                    trace_id = fields.get("task_id") or fields.get("request_id") or fields.get("session_id") or "-"
                    trace = f"，追踪ID={trace_id}" if kind == "node" else ""
                    logger.bind(status="running").info("[{}] {}开始{}", name, label, trace)
                    try:
                        result = await func(*args, **kwargs)
                    except BaseException as exc:
                        _failure_log(kind, name, exc, started, trace_id)
                        raise
                    elapsed = round((perf_counter() - started) * 1000, 3)
                    logger.bind(status="completed", duration_ms=elapsed).info(
                        "[{}] {}完成{}，耗时={:.3f}ms", name, label, trace, elapsed,
                    )
                    return result
            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            fields = trace_fields(args[0] if args else kwargs.get("state"))
            with logger.contextualize(**fields, **{kind: name}):
                started = perf_counter()
                trace_id = fields.get("task_id") or fields.get("request_id") or fields.get("session_id") or "-"
                trace = f"，追踪ID={trace_id}" if kind == "node" else ""
                logger.bind(status="running").info("[{}] {}开始{}", name, label, trace)
                try:
                    result = func(*args, **kwargs)
                except BaseException as exc:
                    _failure_log(kind, name, exc, started, trace_id)
                    raise
                elapsed = round((perf_counter() - started) * 1000, 3)
                logger.bind(status="completed", duration_ms=elapsed).info(
                    "[{}] {}完成{}，耗时={:.3f}ms", name, label, trace, elapsed,
                )
                return result
        return sync_wrapper
    return decorate


def node_log(node_name: str) -> Callable:
    """每个业务节点显式使用；标记供图绑定器检查，避免漏写或重复包装。"""
    def decorate(func: Callable) -> Callable:
        wrapped = _execution_log("node", node_name)(func)
        wrapped.__node_log_name__ = node_name
        return wrapped
    return decorate


def step_log(step_name: str) -> Callable:
    """观察同步/异步步骤，不吞异常，不把协程创建当成执行完成。"""
    return _execution_log("step", step_name)
