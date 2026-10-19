"""BGE-M3：显式本地路径优先，其次仓库名称；设备默认 CPU。"""

from dataclasses import dataclass

from common.config._env import Env, env_bool, env_text, model_source, validate_device


@dataclass(frozen=True)
class EmbeddingConfig:
    bge_m3_path: str | None = None
    bge_m3: str = "BAAI/bge-m3"
    bge_device: str = "cpu"
    bge_fp16: bool = False  # 请求值；实际使用 use_fp16（CPU/MPS 强制 False）

    def __post_init__(self):
        validate_device("BGE_DEVICE", self.bge_device)

    @classmethod
    def from_env(cls, env: Env | None = None):
        return cls(
            bge_m3_path=env_text("BGE_M3_PATH", env=env),
            bge_m3=env_text("BGE_M3", "BAAI/bge-m3", env=env),
            bge_device=env_text("BGE_DEVICE", "cpu", env=env),
            bge_fp16=env_bool("BGE_FP16", False, env=env),
        )

    @property
    def model_name_or_path(self) -> str:
        return model_source(self.bge_m3_path, self.bge_m3, "BGE_M3_PATH", "BGE_M3")

    @property
    def use_fp16(self) -> bool:
        return self.bge_fp16 and self.bge_device.startswith("cuda")


embedding_config = EmbeddingConfig.from_env()
