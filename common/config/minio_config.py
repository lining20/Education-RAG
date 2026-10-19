"""MinIO 配置：endpoint 使用 host[:port]，协议由 MINIO_SECURE 决定。"""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from common.config._env import ConfigError, Env, env_bool, env_text, require


@dataclass(frozen=True)
class MinIOConfig:
    endpoint: str | None = None
    access_key: str | None = field(default=None, repr=False)
    secret_key: str | None = field(default=None, repr=False)
    bucket_name: str | None = None
    minio_img_dir: str = "images"
    minio_secure: bool = False

    @classmethod
    def from_env(cls, env: Env | None = None):
        return cls(
            endpoint=env_text("MINIO_ENDPOINT", env=env),
            access_key=env_text("MINIO_ACCESS_KEY", env=env),
            secret_key=env_text("MINIO_SECRET_KEY", env=env),
            bucket_name=env_text("MINIO_BUCKET_NAME", env=env),
            minio_img_dir=env_text("MINIO_IMG_DIR", "images", env=env),
            minio_secure=env_bool("MINIO_SECURE", False, env=env),
        )

    def validate(self):
        for name, value in (("MINIO_ENDPOINT", self.endpoint), ("MINIO_ACCESS_KEY", self.access_key),
                            ("MINIO_SECRET_KEY", self.secret_key), ("MINIO_BUCKET_NAME", self.bucket_name)):
            require(name, value)
        try:
            parsed = urlsplit("//" + self.endpoint)
            valid = (bool(parsed.hostname) and not parsed.path and not parsed.query
                     and not parsed.fragment and parsed.username is None and parsed.password is None
                     and not any(c.isspace() for c in self.endpoint))
            port = parsed.port
            valid = valid and (port is None or 0 < port <= 65535)
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError("MINIO_ENDPOINT 必须为 host[:port]，不含协议、凭据或路径")


minio_config = MinIOConfig.from_env()
