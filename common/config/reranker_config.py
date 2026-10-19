"""Reranker 配置；LARGE 兼容旧本地路径，新路径请使用 PATH。"""

from dataclasses import dataclass

from common.config._env import Env, env_bool, env_text, model_source, validate_device


@dataclass(frozen=True)
class RerankerConfig:
    bge_reranker_large: str = "BAAI/bge-reranker-large"  # 仓库名或旧本地路径
    bge_reranker_device: str = "cpu"  # cpu / cuda / cuda:N / mps
    bge_reranker_fp16: bool = False  # 请求值；实际使用 use_fp16
    bge_reranker_path: str | None = None

    def __post_init__(self):
        validate_device("BGE_RERANKER_DEVICE", self.bge_reranker_device)

    @classmethod
    def from_env(cls, env: Env | None = None):
        return cls(
            bge_reranker_large=env_text("BGE_RERANKER_LARGE", "BAAI/bge-reranker-large", env=env),
            bge_reranker_device=env_text("BGE_RERANKER_DEVICE", "cpu", env=env),
            bge_reranker_fp16=env_bool("BGE_RERANKER_FP16", False, env=env),
            bge_reranker_path=env_text("BGE_RERANKER_PATH", env=env),
        )

    @property
    def model_name_or_path(self) -> str:
        return model_source(
            self.bge_reranker_path, self.bge_reranker_large,
            "BGE_RERANKER_PATH", "BGE_RERANKER_LARGE",
        )

    @property
    def use_fp16(self) -> bool:
        return self.bge_reranker_fp16 and self.bge_reranker_device.startswith("cuda")


reranker_config = RerankerConfig.from_env()
