"""节点/工具可公开的错误合同；未知异常的原始消息不进入 State 或日志。"""


class OperationError(Exception):
    """message 必须是开发者给出的安全摘要，不能拼接凭据、正文或 SDK 异常。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def error_summary(exc: Exception) -> tuple[str, str, bool]:
    if isinstance(exc, OperationError):
        return exc.code, exc.message, exc.retryable
    return "NODE_EXECUTION_FAILED", "节点执行失败，请通过任务标识核对日志", False
