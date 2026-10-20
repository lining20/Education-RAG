"""httpx 原生异步 SSE 客户端；请求完成后才交还控制，不使用线程池。"""
import asyncio
import json
from time import perf_counter

from test.settings import identity, validate_base_url


async def iter_sse(lines):
    event, data = "message", []
    async for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                yield event, json.loads("\n".join(data))
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].lstrip(" ")
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield event, json.loads("\n".join(data))


class AsyncClient:
    def __init__(self, base_url, timeout=180, concurrency=4, *, transport=None):
        self.base_url = validate_base_url(base_url)
        self.timeout = timeout
        self.concurrency = concurrency
        self.transport = transport
        self.http = None

    async def __aenter__(self):
        import httpx  # 项目已有依赖；只做默认离线预览时不加载网络客户端。
        self.http = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout,
                                     limits=httpx.Limits(max_connections=self.concurrency,
                                                        max_keepalive_connections=self.concurrency),
                                     follow_redirects=False, trust_env=False, transport=self.transport)
        return self

    async def __aexit__(self, *args):
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    async def verify(self):
        response = await self.http.get("/api/test/identity")
        response.raise_for_status()
        actual = response.json()
        if {key: actual.get(key) for key in identity()} != identity():
            raise RuntimeError("不是同一数据版本的隔离测试服务，请重启 test.settings --serve")
        return actual

    async def query(self, payload):
        # 调用方必须已取得并发名额，并已 await 前置对话完成；此处只计本次请求。
        started = perf_counter()
        capture = {"events": [], "result": None, "error": None, "first_delta_ms": None,
                   "done": False, "elapsed_ms": None}
        try:
            async with asyncio.timeout(self.timeout):
                async with self.http.stream("POST", "/api/query/stream", json=payload) as response:
                    response.raise_for_status()
                    if response.headers.get("content-type", "").split(";", 1)[0].strip() != "text/event-stream":
                        raise ValueError("响应不是 SSE")
                    async for event, item in iter_sse(response.aiter_lines()):
                        capture["events"].append({"event": event, "data": item})
                        if event == "delta" and item.get("text") and capture["first_delta_ms"] is None:
                            capture["first_delta_ms"] = round((perf_counter() - started) * 1000, 3)
                        if event == "error":
                            capture["error"] = item
                        elif event == "result":
                            if capture["result"] is not None:
                                raise ValueError("重复 result 事件")
                            capture["result"] = item
                        elif event == "done":
                            capture["done"] = True
                            break
                if not capture["done"] or (capture["result"] is None and capture["error"] is None):
                    raise ValueError("SSE 缺少终止事件或最终结果")
        except Exception as exc:
            capture["error"] = capture["error"] or {"code": "CLIENT_OR_PROTOCOL_ERROR",
                                                       "exception_type": type(exc).__name__}
        # CancelledError 不在这里吞掉，取消会通过流上下文关闭连接。
        capture["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
        return capture
