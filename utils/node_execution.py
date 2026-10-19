"""导入图统一执行边界：依赖绑定与异常转 State。

不实现业务节点，不创建任务/客户端，不发送旧 SSE。依赖由调用方注入。
"""

from dataclasses import dataclass
from typing import Any, Callable

from common.errors import OperationError, error_summary
from common.logging.logger import logger, trace_fields
from utils.task_utils import ImportTaskStore, track_node_progress


@dataclass(frozen=True)
class ImportDependencies:
    tasks: ImportTaskStore
    max_file_bytes: int = 100 * 1024 * 1024  # Service 可覆盖的运行策略。

    def __post_init__(self):
        if (isinstance(self.max_file_bytes, bool) or not isinstance(self.max_file_bytes, int)
                or self.max_file_bytes <= 0):
            raise ValueError("max_file_bytes 必须为正整数")


def bind_node(name: str, node: Callable, deps: ImportDependencies) -> Callable:
    """适配已审核图工厂；运行/完成列表由节点主函数显式更新。

    这里只转换异常，并给本次成功领取的阶段记失败，不重复登记运行/完成。
    CancelledError 等 BaseException 直接传播，由 Service/finally 收尾。
    """
    if not callable(node) or not isinstance(deps, ImportDependencies):
        raise TypeError("必须注入可调用节点和 ImportDependencies")
    if getattr(node, "__node_log_name__", None) != f"node_{name}":
        raise TypeError(f'节点必须显式使用 @node_log("node_{name}")，不能省略')

    async def bound(state: dict[str, Any]) -> dict[str, Any]:
        previous_error = state.get("error")
        if name != "handle_error" and (previous_error is not None or state.get("status") == "failed"):
            return {"status": "failed", "error": previous_error}
        # 保留前序 current_node，供节点第一步核对阶段衔接。
        view = {**state}
        produced_update = {}
        business_returned = False
        with track_node_progress() as claimed, logger.contextualize(flow="import", **trace_fields(state)):
            try:
                # 节点自身已声明 node_log；绑定器不代加装饰器或更新成功进度。
                update = await node(view, deps=deps)
                business_returned = True
                if not isinstance(update, dict):
                    raise OperationError("INVALID_NODE_RESULT", "节点必须返回增量状态字典")
                produced_update = update
                if name != "handle_error" and (update.get("error") is not None or update.get("status") == "failed"):
                    raise OperationError("INVALID_NODE_RESULT", "普通节点须通过异常报告失败，由统一执行层生成错误状态")
                return {**update, "current_node": name}
            except Exception as exc:
                code, message, retryable = error_summary(exc)
                if business_returned:
                    # 业务异常由节点装饰器记栈；非法返回由执行边界报告。
                    logger.error("节点执行边界失败 node={} code={} message={}", name, code, message)
                error = previous_error if previous_error is not None else {
                    "node": name, "code": code, "message": message, "retryable": retryable,
                }
                warnings = list(produced_update.get("warnings", state.get("warnings", [])))
                if name in claimed:
                    try:
                        await deps.tasks.fail_node(state["task_id"], state["document_id"], name, error)
                    except Exception:
                        warnings.append({"node": name, "code": "TASK_PROGRESS_UPDATE_FAILED",
                                         "message": "节点失败进度未能保存到内存"})
                        logger.bind(node=name).warning("节点失败进度未能保存；原始错误已保留")
                # entry 现于第一步领取；之后校验失败仍属于本次任务，保留收尾资格。
                if name == "entry" and name in claimed:
                    produced_update["task_claimed"] = True
                return {**produced_update, "current_node": name, "status": "failed", "error": error, "warnings": warnings,
                        "result": {"status": "failed", "error": error}}
    return bound
