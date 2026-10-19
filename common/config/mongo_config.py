"""MongoDB 集中配置，沿用 MONGO_URL / MONGO_DB_NAME；不导入旧历史工具。"""

from dataclasses import dataclass, field

from common.config._env import ConfigError, Env, env_int, env_text, require, validate_url


@dataclass(frozen=True)
class MongoConfig:
    mongo_url: str | None = field(default=None, repr=False)
    db_name: str | None = None
    import_tasks_collection: str = "import_tasks"
    documents_collection: str = "documents"
    content_records_collection: str = "content_records"
    course_relations_collection: str = "course_relations"
    conversations_collection: str = "conversations"
    chat_message_collection: str = "chat_message"
    connect_timeout_ms: int = 10000
    server_selection_timeout_ms: int = 10000
    socket_timeout_ms: int = 10000

    @classmethod
    def from_env(cls, env: Env | None = None):
        values = {
            name: env_text("MONGO_" + name.upper(), default, env=env)
            for name, default in (
                ("import_tasks_collection", "import_tasks"), ("documents_collection", "documents"),
                ("content_records_collection", "content_records"), ("course_relations_collection", "course_relations"),
                ("conversations_collection", "conversations"), ("chat_message_collection", "chat_message"),
            )
        }
        return cls(
            mongo_url=env_text("MONGO_URL", env=env),
            db_name=env_text("MONGO_DB_NAME", env=env),
            connect_timeout_ms=env_int("MONGO_CONNECT_TIMEOUT_MS", 10000, env=env),
            server_selection_timeout_ms=env_int("MONGO_SERVER_SELECTION_TIMEOUT_MS", 10000, env=env),
            socket_timeout_ms=env_int("MONGO_SOCKET_TIMEOUT_MS", 10000, env=env),
            **values,
        )

    def validate(self):
        validate_url("MONGO_URL", self.mongo_url, ("mongodb", "mongodb+srv"))
        require("MONGO_DB_NAME", self.db_name)
        if len(self.db_name.encode("utf-8")) >= 64 or any(c in self.db_name for c in '/\\. "$*<>:|?\x00'):
            raise ConfigError("MONGO_DB_NAME 含非法字符或长度达到 64 字节")
        names = []
        for attr in ("import_tasks_collection", "documents_collection", "content_records_collection",
                     "course_relations_collection", "conversations_collection", "chat_message_collection"):
            name = getattr(self, attr)
            require("MONGO_" + attr.upper(), name)
            if "$" in name or "\x00" in name or name.startswith("system."):
                raise ConfigError("MONGO_" + attr.upper() + " 含不允许的集合名称")
            names.append(name)
        if len(set(names)) != len(names):
            raise ConfigError("MongoDB 六类业务集合必须使用不同名称")


mongo_config = MongoConfig.from_env()
