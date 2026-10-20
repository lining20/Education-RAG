"""Milvus 连接及四个教育集合；不创建或修改服务端集合。"""

from dataclasses import dataclass
import re

from common.config._env import ConfigError, Env, env_float, env_text, validate_url


@dataclass(frozen=True)
class MilvusConfig:
    milvus_url: str | None = None
    document_index_collection: str = "education_document_index"
    courses_collection: str = "education_courses"
    document_chunks_collection: str = "education_document_chunks"
    questions_collection: str = "education_questions"
    timeout: float = 10.0  # 秒

    def __post_init__(self):
        names = (self.document_index_collection, self.courses_collection, self.document_chunks_collection, self.questions_collection)
        for variable, name in zip(("DOCUMENT_INDEX_COLLECTION", "COURSES_COLLECTION", "DOCUMENT_CHUNKS_COLLECTION", "QUESTIONS_COLLECTION"), names):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,254}", name):
                raise ConfigError(f"{variable} 必须为 1-255 位字母、数字或下划线，不能以数字开头")
        if len(set(names)) != 4:
            raise ConfigError("一级索引、课程、文档、题目四个 Milvus 集合必须使用不同名称")

    @classmethod
    def from_env(cls, env: Env | None = None):
        return cls(
            milvus_url=env_text("MILVUS_URL", env=env),
            document_index_collection=env_text("DOCUMENT_INDEX_COLLECTION", "education_document_index", env=env),
            courses_collection=env_text("COURSES_COLLECTION", "education_courses", env=env),
            document_chunks_collection=env_text("DOCUMENT_CHUNKS_COLLECTION", "education_document_chunks", env=env),
            questions_collection=env_text("QUESTIONS_COLLECTION", "education_questions", env=env),
            timeout=env_float("MILVUS_TIMEOUT", 10.0, positive=True, env=env),
        )

    def validate(self):
        validate_url("MILVUS_URL", self.milvus_url)


milvus_config = MilvusConfig.from_env()
