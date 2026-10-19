"""LLM 配置；仅在创建客户端时要求地址和密钥。"""

from dataclasses import dataclass, field

from common.config._env import Env, env_float, env_text, require, validate_url


@dataclass(frozen=True)
class LLMConfig:
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    vl_model: str | None = None
    llm_model: str = "qwen3-32b"
    llm_temperature: float = 0.1

    @classmethod
    def from_env(cls, env: Env | None = None):
        return cls(
            base_url=env_text("OPENAI_BASE_URL", env=env),
            api_key=env_text("OPENAI_API_KEY", env=env),
            vl_model=env_text("VL_MODEL", env=env),
            llm_model=env_text("LLM_DEFAULT_MODEL", "qwen3-32b", env=env),
            llm_temperature=env_float("LLM_DEFAULT_TEMPERATURE", 0.1, minimum=0, maximum=2, env=env),
        )

    def validate(self):
        require("OPENAI_API_KEY", self.api_key)
        validate_url("OPENAI_BASE_URL", self.base_url)
        require("LLM_DEFAULT_MODEL", self.llm_model)


lm_config = LLMConfig.from_env()
