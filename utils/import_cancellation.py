"""导入关闭信号：异步节点和同步解析线程共用同一 Event。"""
from common.errors import OperationError


def check_import_cancelled(event) -> None:
    if event is not None and event.is_set():
        raise OperationError("IMPORT_CANCELLED", "服务正在关闭，导入已中断，请重新上传", retryable=True)
