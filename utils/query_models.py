"""检索 LLM 和可选网络搜索，复用现有客户端、提示词和请求限流。"""
import asyncio
import json
from urllib.parse import urlsplit
from langchain_core.messages import HumanMessage, SystemMessage
from common.errors import OperationError
from common.logging.logger import logger
from utils.load_prompt import load_prompt
from utils.rate_limit_utils import apply_api_rate_limit


def _model_error(exc, *, stage, code, message):
    """记录可公开的 HTTP 状态和参数名，不回显模型请求、凭据或响应正文。"""
    status = getattr(exc, "status_code", None)
    status = status if isinstance(status, int) and 400 <= status <= 599 else None
    allowed = {"temperature", "enable_thinking", "thinking", "reasoning_effort", "response_format",
               "model", "messages", "max_tokens", "max_completion_tokens", "top_p"}
    parameter = getattr(exc, "param", None)
    parameter = parameter if isinstance(parameter, str) and parameter in allowed else "未知"
    logger.error("检索模型请求失败：阶段={}，HTTP={}，异常类型={}，参数={}",
                 stage, status or "未知", type(exc).__name__, parameter)
    if status is not None:
        message += f"（HTTP {status}" + (f"，参数 {parameter}" if parameter != "未知" else "") + "）"
    retryable = status is None or status in {408, 409, 429} or status >= 500
    return OperationError(code, message, retryable=retryable)


class QueryLLM:
    def __init__(self, limiter):
        self.limiter = limiter

    async def complete(self, prompt_name, **variables):
        from utils.lm.lm_utils import get_llm_client
        await apply_api_rate_limit(self.limiter)
        client = get_llm_client(json_mode=prompt_name == "query_rewrite", max_retries=0)
        try:
            response = await asyncio.wait_for(client.ainvoke([
                SystemMessage(content=load_prompt(prompt_name)),
                HumanMessage(content=json.dumps(variables, ensure_ascii=False))]), timeout=60)
            return response.content
        except Exception as exc:
            raise _model_error(exc, stage=prompt_name, code="QUERY_MODEL_FAILED",
                               message="检索模型调用失败，请检查模型配置和服务") from exc

    async def stream(self, **variables):
        from utils.lm.lm_utils import get_llm_client
        await apply_api_rate_limit(self.limiter)
        client = get_llm_client(max_retries=0)
        try:
            async with asyncio.timeout(120):
                async for chunk in client.astream([
                    SystemMessage(content=load_prompt("query_answer")),
                    HumanMessage(content=json.dumps(variables, ensure_ascii=False))]):
                    if isinstance(chunk.content, str) and chunk.content:
                        yield chunk.content
        except Exception as exc:
            raise _model_error(exc, stage="query_answer", code="ANSWER_MODEL_FAILED",
                               message="回答生成中断，请检查模型服务后重试") from exc


async def search_web(query: str) -> list[dict]:
    """沿用参考项目：Streamable HTTP MCP → bailian_web_search(query, count=5)。"""
    from agents.mcp import MCPServerStreamableHttp
    from common.config.bailian_mcp_config import mcp_config
    mcp_config.validate()
    try:
        # 同一协程建立/释放 MCP 连接，不在异步图里使用 asyncio.run。
        async with MCPServerStreamableHttp(
            name="web_search_docs",
            params={"url": mcp_config.mcp_base_url,
                    "headers": {"Authorization": f"Bearer {mcp_config.api_key}"}, "timeout": 50},
            client_session_timeout_seconds=50,
            cache_tools_list=True, max_retry_attempts=3,
        ) as client:
            result = await client.call_tool(tool_name="bailian_web_search", arguments={"query": query, "count": 5})
        if getattr(result, "isError", False):
            raise OperationError("WEB_SEARCH_FAILED", "百炼联网搜索 MCP 工具返回失败，请检查服务配置", retryable=True)
        data = getattr(result, "structuredContent", None)
        if not isinstance(data, dict) or "pages" not in data:
            data = None
            for item in result.content:
                if getattr(item, "type", None) != "text":
                    continue
                try:
                    value = json.loads(item.text)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict) and "pages" in value:
                    data = value
                    break
        if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
            raise OperationError("INVALID_WEB_SEARCH_RESULT", "百炼联网搜索未返回预期的 pages 列表")
        candidates = []
        for page in data["pages"][:5]:
            if not isinstance(page, dict):
                continue
            url = page.get("url") or page.get("link")
            title = page.get("title") or "网页"
            content = page.get("text") or page.get("content") or page.get("snippet") or page.get("summary") or ""
            if not isinstance(url, str) or not isinstance(title, str) or not isinstance(content, str):
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    continue
            except ValueError:
                continue
            candidates.append({"key": "web:" + url, "kind": "web", "title": title,
                               "url": url, "content": content[:6000]})
        logger.info("百炼联网搜索完成：返回网页数={}，有效候选数={}", len(data["pages"]), len(candidates))
        return candidates
    except OperationError:
        raise
    except Exception as exc:
        logger.warning("百炼联网搜索调用失败：异常类型={}", type(exc).__name__)
        raise OperationError("WEB_SEARCH_FAILED", "百炼联网搜索失败，本次仅使用可用知识库结果", retryable=True) from exc
