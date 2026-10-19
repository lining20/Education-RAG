"""暂不接入业务流程的可选 MCP 配置。"""

from dataclasses import dataclass, field

from common.config._env import Env, env_text, require, validate_url


@dataclass(frozen=True)
class McpConfig:
    mcp_base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Env | None = None):
        return cls(
            mcp_base_url=env_text("MCP_DASHSCOPE_BASE_URL", env=env),
            api_key=env_text("OPENAI_API_KEY", env=env),
        )

    def validate(self):
        validate_url("MCP_DASHSCOPE_BASE_URL", self.mcp_base_url)
        require("OPENAI_API_KEY", self.api_key)


mcp_config = McpConfig.from_env()
