"""百炼联网搜索 MCP 配置；沿用参考项目的地址和 Bearer 鉴权。"""
from dataclasses import dataclass, field
from common.config._env import Env, env_text, validate_url
from common.errors import OperationError


@dataclass(frozen=True)
class McpConfig:
    mcp_base_url: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Env | None = None):
        return cls(mcp_base_url=env_text("MCP_DASHSCOPE_BASE_URL", env=env),
                   api_key=env_text("MCP_DASHSCOPE_API_KEY", env=env) or env_text("OPENAI_API_KEY", env=env))

    def validate(self):
        if not self.mcp_base_url or not self.api_key:
            raise OperationError("WEB_SEARCH_NOT_CONFIGURED", "百炼联网搜索 MCP 未配置，请检查 MCP_DASHSCOPE_BASE_URL 和 MCP 鉴权密钥")
        validate_url("MCP_DASHSCOPE_BASE_URL", self.mcp_base_url)


mcp_config = McpConfig.from_env()
