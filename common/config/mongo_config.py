"""MongoDB 保存按 session_id 关联的聊天历史及独立上传账号/登录会话；不保存导入任务和知识内容。"""
from dataclasses import dataclass, field
from common.config._env import ConfigError, Env, env_int, env_text, require, validate_url


@dataclass(frozen=True)
class MongoConfig:
    mongo_url: str | None = field(default=None, repr=False)
    db_name: str | None = None
    chat_message_collection: str = "chat_message"
    connect_timeout_ms: int = 10000
    server_selection_timeout_ms: int = 10000
    socket_timeout_ms: int = 10000

    @classmethod
    def from_env(cls, env: Env | None = None):
        return cls(
            mongo_url=env_text("MONGO_URL", env=env),
            db_name=env_text("MONGO_DB_NAME", env=env),
            chat_message_collection=env_text("MONGO_CHAT_MESSAGE_COLLECTION", "chat_message", env=env),
            connect_timeout_ms=env_int("MONGO_CONNECT_TIMEOUT_MS", 10000, env=env),
            server_selection_timeout_ms=env_int("MONGO_SERVER_SELECTION_TIMEOUT_MS", 10000, env=env),
            socket_timeout_ms=env_int("MONGO_SOCKET_TIMEOUT_MS", 10000, env=env),
        )

    def validate(self):
        validate_url("MONGO_URL", self.mongo_url, ("mongodb", "mongodb+srv"))
        require("MONGO_DB_NAME", self.db_name)
        if len(self.db_name.encode("utf-8")) >= 64 or any(c in self.db_name for c in '/\\. "$*<>:|?\x00'):
            raise ConfigError("MONGO_DB_NAME 含非法字符或长度达到 64 字节")
        name = self.chat_message_collection
        require("MONGO_CHAT_MESSAGE_COLLECTION", name)
        if "$" in name or "\x00" in name or name.startswith("system."):
            raise ConfigError("MONGO_CHAT_MESSAGE_COLLECTION 含不允许的集合名称")


mongo_config = MongoConfig.from_env()
