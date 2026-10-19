"""PDF 解析服务配置；未使用服务时无需 token，不导入解析 SDK。"""

from dataclasses import dataclass, field

from common.config._env import Env, env_float, env_int, env_text, require, validate_url


@dataclass(frozen=True)
class MineruConfig:
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    request_timeout: float = 30.0  # 秒；供后续 PDF 适配器使用
    poll_interval: float = 2.0
    max_poll_attempts: int = 60

    @classmethod
    def from_env(cls, env: Env | None = None):
        return cls(
            base_url=env_text("MINERU_BASE_URL", env=env),
            api_key=env_text("MINERU_API_TOKEN", env=env),
            request_timeout=env_float("MINERU_REQUEST_TIMEOUT", 30.0, positive=True, env=env),
            poll_interval=env_float("MINERU_POLL_INTERVAL", 2.0, positive=True, env=env),
            max_poll_attempts=env_int("MINERU_MAX_POLL_ATTEMPTS", 60, env=env),
        )

    def validate(self):
        validate_url("MINERU_BASE_URL", self.base_url)
        require("MINERU_API_TOKEN", self.api_key)


mineru_config = MineruConfig.from_env()
