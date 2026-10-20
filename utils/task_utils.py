"""单进程导入任务与节点进度；线程锁保护，状态仅保存在内存中。

Service 创建 pending 任务，节点在主函数首尾显式调用进度工具。不连接 MongoDB；
进程重启后记录丢失，任务查询应报告不存在，不能把内存用作知识库发布依据。
"""
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import re
from threading import RLock
from time import perf_counter
from typing import Any

from common.errors import OperationError


# 只跟踪本次调用成功领取的阶段，防止重复调用者在异常时改写他人的进度。
_claimed_nodes: ContextVar[set[str] | None] = ContextVar("claimed_import_nodes", default=None)


@contextmanager
def track_node_progress():
    claimed: set[str] = set()
    token = _claimed_nodes.set(claimed)
    try:
        yield claimed
    finally:
        _claimed_nodes.reset(token)


def validate_identifier(value: Any, field: str) -> str:
    """沿用字符串 ID 合同，不强制 UUID，不静默修正调用方输入。"""
    if (not isinstance(value, str) or not value or value != value.strip()
            or not value.isprintable() or len(value.encode("utf-8")) > 64):
        raise OperationError("INVALID_IDENTIFIER", f"{field} 必须为非空、无首尾空白的字符串，最多 64 个 UTF-8 字节")
    return value


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class ImportTaskStore:
    """注入同一个实例以共享任务进度；所有快照深拷贝，不暴露内部可变容器。"""

    def __init__(self):
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def create_pending(self, task_id: str, document_id: str) -> None:
        """由 Service 在调度前调用；重复任务不得覆盖已有运行状态。"""
        validate_identifier(task_id, "task_id")
        validate_identifier(document_id, "document_id")
        with self._lock:
            if task_id in self._tasks:
                raise OperationError("TASK_STATE_CONFLICT", "任务已存在，不能重复创建")
            self._tasks[task_id] = {
                "task_id": task_id, "document_id": document_id, "status": "pending",
                "current_node": "", "counts": {}, "node_progress": {}, "result": {},
                "created_at": _now_ms(),
            }

    def _get_task(self, task_id: str, document_id: str) -> dict:
        validate_identifier(task_id, "task_id")
        validate_identifier(document_id, "document_id")
        task = self._tasks.get(task_id)
        if task is None:
            raise OperationError("TASK_NOT_FOUND", "内存任务不存在；请先创建任务或重新提交")
        if task["document_id"] != document_id:
            raise OperationError("TASK_DOCUMENT_MISMATCH", "任务与来源文档不匹配")
        return task

    @staticmethod
    def _validate_node(node: str) -> None:
        if not isinstance(node, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", node):
            raise ValueError("节点注册键格式非法")

    async def start_pending(self, task_id: str, document_id: str, counts: dict) -> None:
        """原子领取 pending 任务；并发重复启动只能成功一次。"""
        with self._lock:
            task = self._get_task(task_id, document_id)
            if task["status"] != "pending":
                raise OperationError("TASK_STATE_CONFLICT", "任务已非 pending，不能重复启动或重置")
            now = _now_ms()
            task.update(status="running", current_node="entry", counts=deepcopy(counts),
                        started_at=now, updated_at=now)
            task["node_progress"]["entry"] = {"status": "running", "started_at": now}

    def reject_pending(self, task_id: str, document_id: str, error: dict) -> bool:
        """输入校验失败时结束仍未被领取的任务；不影响并发运行者或授予清理资格。"""
        with self._lock:
            try:
                task = self._get_task(task_id, document_id)
            except OperationError:
                return False  # 无效/不存在/归属不符的标识不能修改任何任务。
            if task["status"] != "pending":
                return False
            now = _now_ms()
            task.update(status="failed", current_node="entry", finished_at=now, updated_at=now,
                        result={"status": "failed", "error": deepcopy(error)})
            task["node_progress"]["entry"] = {"status": "failed", "finished_at": now, "error": deepcopy(error)}
            return True

    async def start_node(self, task_id: str, document_id: str, node: str, previous_node: str) -> None:
        """上一阶段完成后才领取下一阶段，不覆盖其他执行中的进度。"""
        self._validate_node(node)
        self._validate_node(previous_node)
        with self._lock:
            task = self._get_task(task_id, document_id)
            previous_status = task["node_progress"].get(previous_node, {}).get("status")
            allowed = previous_status == "completed" or (node == "handle_error" and previous_status == "failed")
            if (task["status"] != "running" or task["current_node"] != previous_node or not allowed):
                raise OperationError("TASK_STATE_CONFLICT", "前序阶段尚未完成或已被其他调用推进")
            now = _now_ms()
            task.update(current_node=node, updated_at=now)
            task["node_progress"][node] = {"status": "running", "started_at": now}

    def _running_stage(self, task_id: str, document_id: str, node: str) -> dict:
        self._validate_node(node)
        task = self._get_task(task_id, document_id)
        if (task["status"] != "running" or task["current_node"] != node
                or task["node_progress"].get(node, {}).get("status") != "running"):
            raise OperationError("TASK_STATE_CONFLICT", "任务阶段已改变，不能覆盖当前进度")
        return task

    async def complete_node(self, task_id: str, document_id: str, node: str,
                            duration_ms: float, counts: dict | None = None) -> None:
        """节点完成不等于整个任务成功；只记录阶段完成及耗时。"""
        with self._lock:
            task = self._running_stage(task_id, document_id, node)
            now = _now_ms()
            task["node_progress"][node].update(status="completed", finished_at=now, duration_ms=duration_ms)
            if counts is not None:
                task["counts"] = deepcopy(counts)
            task["updated_at"] = now

    async def fail_node(self, task_id: str, document_id: str, node: str, error: dict) -> None:
        """仅已领取的运行阶段可记失败；重复启动者不能修改他人的任务。"""
        with self._lock:
            task = self._running_stage(task_id, document_id, node)
            now = _now_ms()
            task["node_progress"][node].update(status="failed", finished_at=now, error=deepcopy(error))
            task["updated_at"] = now

    def set_task_result(self, task_id: str, document_id: str, status: str, result: dict) -> None:
        """最终节点/执行层收尾使用；发布到知识库仍须另写 MinIO 成功清单。"""
        if status not in {"succeeded", "failed"}:
            raise ValueError("最终任务状态只能是 succeeded 或 failed")
        with self._lock:
            task = self._get_task(task_id, document_id)
            phase = task["node_progress"].get(task["current_node"], {}).get("status")
            if (task["status"] != "running" or
                    (status == "succeeded" and (task["current_node"] != "finish" or phase != "completed")) or
                    (status == "failed" and phase != "failed" and not
                     (task["current_node"] == "handle_error" and phase == "completed"))):
                raise OperationError("TASK_STATE_CONFLICT", "当前执行没有结束此任务的条件")
            task.update(status=status, result=deepcopy(result), finished_at=_now_ms())

    def get(self, task_id: str) -> dict:
        """返回状态及运行/完成/失败节点列表；重启后不存在的任务明确报错。"""
        validate_identifier(task_id, "task_id")
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise OperationError("TASK_NOT_FOUND", "内存任务不存在，服务重启后请重新提交")
            result = deepcopy(task)
            for key, status in (("running_nodes", "running"), ("completed_nodes", "completed"), ("failed_nodes", "failed")):
                result[key] = [name for name, progress in task["node_progress"].items() if progress["status"] == status]
            return result

    def abort_task(self, task_id: str, document_id: str, error: dict) -> None:
        """仅供服务执行器兜底；保留真实节点失败，避免异常退出后永久运行。"""
        with self._lock:
            task = self._get_task(task_id, document_id)
            if task["status"] in {"succeeded", "failed"}:
                return
            now = _now_ms()
            for progress in task["node_progress"].values():
                if progress["status"] == "running":
                    progress.update(status="failed", error=deepcopy(error), finished_at=now)
            task.update(status="failed", finished_at=now,
                        result={"status": "failed", "error": deepcopy(error)})

    def clear(self, task_id: str) -> None:
        """由 Service 在保留期结束后释放终态任务，避免进程内无限积累。"""
        with self._lock:
            task = self.get(task_id)
            if task["status"] not in {"succeeded", "failed"}:
                raise OperationError("TASK_STATE_CONFLICT", "不能清除尚未结束的任务")
            del self._tasks[task_id]


async def add_running_task(state: dict, node: str, *, tasks: ImportTaskStore) -> float:
    """在节点第一步登记运行；entry 同时原子领取 pending 任务。"""
    if node == "entry":
        await tasks.start_pending(state["task_id"], state["document_id"], state["counts"])
    else:
        await tasks.start_node(state["task_id"], state["document_id"], node, state["current_node"])
    claimed = _claimed_nodes.get()
    if claimed is not None:
        claimed.add(node)
    return perf_counter()


async def add_done_task(state: dict, node: str, *, tasks: ImportTaskStore,
                        started_at: float, counts: dict | None = None) -> None:
    """全部业务步骤成功后调用：移出运行列表，加入已完成列表。"""
    await tasks.complete_node(state["task_id"], state["document_id"], node,
                              round((perf_counter() - started_at) * 1000, 3), counts)
