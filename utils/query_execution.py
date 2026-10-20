"""查询依赖、节点绑定和运行/完成列表，与导入任务状态分别管理。"""
import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable
from common.errors import error_summary
from common.logging.logger import logger


async def run_sync(function, *args, **kwargs):
    """取消时等待当前同步 SDK/模型调用退出，避免遗留后台写入。"""
    work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        await work
        raise


class QueryProgress:
    def __init__(self):
        self.nodes = {}

    def snapshot(self):
        return {"running_nodes": [k for k, v in self.nodes.items() if v["status"] == "running"],
                "completed_nodes": [k for k, v in self.nodes.items() if v["status"] == "completed"],
                "failed_nodes": [k for k, v in self.nodes.items() if v["status"] == "failed"],
                "node_progress": {k: dict(v) for k, v in self.nodes.items()}}


@dataclass(frozen=True)
class QueryDependencies:
    retrieval: Any
    history: Any
    llm: Any
    embed: Callable
    rerank: Callable
    web_search: Callable
    emit: Callable
    progress: QueryProgress


async def add_running_task(state, node, *, deps):
    deps.progress.nodes[node] = {"status": "running"}
    await deps.emit("progress", {"node": node, **deps.progress.snapshot()})
    return perf_counter()


async def add_done_task(state, node, *, deps, started_at):
    deps.progress.nodes[node] = {"status": "completed", "duration_ms": round((perf_counter() - started_at) * 1000, 2)}
    await deps.emit("progress", {"node": node, **deps.progress.snapshot()})


def bind_query_node(name, function, deps):
    if getattr(function, "__node_log_name__", None) != "node_" + name:
        raise TypeError("检索节点必须显式声明 @node_log")
    async def bound(state):
        if state.get("errors") and name != "handle_error":
            return {}
        with logger.contextualize(flow="query", request_id=state["request_id"], session_id=state["session_id"]):
            try:
                return await function(state, deps=deps)
            except Exception as exc:
                code, message, retryable = error_summary(exc)
                deps.progress.nodes[name] = {"status": "failed", "code": code}
                await deps.emit("progress", {"node": name, **deps.progress.snapshot()})
                return {"errors": [{"node": name, "code": code, "message": message, "retryable": retryable}]}
    return bound
