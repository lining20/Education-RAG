"""参考项目的滑动窗口限流；窗口有额度立即放行，满额才异步等待。"""
import asyncio
from collections import deque
from time import monotonic

from common.logging.logger import logger
from utils.import_cancellation import check_import_cancelled


class ApiRateLimiter:
    """同一服务的所有文档共享实例；只对请求发起限速，不等待响应串行完成。"""
    def __init__(self, max_requests: int = 3000, window_seconds: float = 60):
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("请求次数和限流窗口必须大于 0")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._times = deque()
        self._lock = asyncio.Lock()

    async def acquire(self, *, stop_event=None):
        logged = False
        while True:
            check_import_cancelled(stop_event)
            async with self._lock:
                now = monotonic()
                while self._times and now - self._times[0] >= self.window_seconds:
                    self._times.popleft()
                delay = 0
                if len(self._times) >= self.max_requests:
                    delay = self._times[0] + self.window_seconds - now
                if delay <= 0:
                    self._times.append(now)
                    logger.debug("API请求时间戳已记录，当前{}秒窗口内请求数：{}", self.window_seconds, len(self._times))
                    return
            if not logged:
                logger.debug("模型请求限流：{}秒内最多{}次，等待约{:.2f}秒", self.window_seconds, self.max_requests, delay)
                logged = True
            # 等待不持锁，每 0.1 秒检查关闭信号，Ctrl+C 不必等完整限流窗口。
            await asyncio.sleep(min(delay, 0.1))


async def apply_api_rate_limit(limiter: ApiRateLimiter, *, stop_event=None) -> None:
    """在实际模型请求前显式调用；Service 注入全局共享的配额实例。"""
    await limiter.acquire(stop_event=stop_event)
