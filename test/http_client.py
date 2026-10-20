"""现有 HTTP API 的轻量客户端；不导入业务模块，不自动重试。"""
import http.cookiejar
import json
from time import perf_counter
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener

from test.settings import identity, validate_base_url


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def iter_sse(lines):
    event, data = "message", []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
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


class Client:
    def __init__(self, base_url, timeout=180):
        self.base_url = validate_base_url(base_url)
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect(),
                                   HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def open(self, path, *, method="GET", data=None, content_type=None):
        if not path.startswith("/api/") or path.startswith("//"):
            raise ValueError("仅允许固定 API 路径")
        request = Request(self.base_url + path, data=data, method=method)
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            return self.opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            # 不保存请求头、登录密码或可能带凭据的异常原文。
            raise RuntimeError(f"API 返回 HTTP {exc.code}：{path}") from None

    def json(self, path, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with self.open(path, method="GET" if payload is None else "POST", data=data,
                       content_type="application/json" if data else None) as response:
            return json.load(response)

    def verify(self):
        actual = self.json("/api/test/identity")
        if {key: actual.get(key) for key in identity()} != identity():
            raise RuntimeError("不是同一数据版本的隔离测试服务，请使用 python -B -m test.settings --serve")
        return actual

    def query(self, payload):
        started = perf_counter()
        capture = {"events": [], "result": None, "error": None, "first_delta_ms": None,
                   "done": False, "elapsed_ms": None}
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            with self.open("/api/query/stream", method="POST", data=data,
                           content_type="application/json") as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise ValueError("响应不是 SSE")
                for event, item in iter_sse(response):
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
        capture["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
        return capture


def successful(capture):
    return (bool(capture.get("done")) and not capture.get("error")
            and (capture.get("result") or {}).get("status") in
            {"completed", "no_results", "insufficient_evidence", "clarification_required"})
