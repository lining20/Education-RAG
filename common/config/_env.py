"""共享配置读取：固定根目录、进程变量优先、空白回退、错误不回显凭据。

from_env({}) 可构建隔离的默认配置；from_env() 使用已加载的进程环境。
必填服务参数由 validate() 在服务初始化前检查，模块导入不会联网。
"""

from collections.abc import Mapping
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from dotenv import load_dotenv

from utils.path_util import PROJECT_ROOT

Env = Mapping[str, str | None]


class ConfigError(ValueError):
    """配置缺失或格式错误（消息仅含变量名与约束）。"""


def load_environment(root: Path = PROJECT_ROOT) -> None:
    # 显式路径，不搜索工作目录的其他 .env；不覆盖进程环境。
    load_dotenv(root / ".env", override=False, encoding="utf-8-sig")


load_environment()


def env_text(name: str, default: str | None = None, *, env: Env | None = None) -> str | None:
    value = (os.environ if env is None else env).get(name)
    return value.strip() if value is not None and value.strip() else default


def env_bool(name: str, default: bool = False, *, env: Env | None = None) -> bool:
    value = env_text(name, env=env)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} 必须为 true/false、1/0、yes/no 或 on/off")


def env_float(name: str, default: float, *, minimum: float | None = None,
              maximum: float | None = None, positive: bool = False, env: Env | None = None) -> float:
    try:
        value = float(env_text(name, env=env) or str(default))
    except ValueError:
        raise ConfigError(f"{name} 必须为有限数字") from None
    if not math.isfinite(value):
        raise ConfigError(f"{name} 必须为有限数字")
    if positive and value <= 0:
        raise ConfigError(f"{name} 必须大于 0")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ConfigError(f"{name} 必须在 {minimum} 到 {maximum} 范围内")
    return value


def env_int(name: str, default: int, *, env: Env | None = None) -> int:
    try:
        value = int(env_text(name, env=env) or str(default))
    except ValueError:
        raise ConfigError(f"{name} 必须为正整数") from None
    if value <= 0:
        raise ConfigError(f"{name} 必须为正整数")
    return value


def require(name: str, value: str | None) -> None:
    if value is None or not value.strip():
        raise ConfigError(f"缺少必要配置 {name}")


def validate_url(name: str, value: str | None, schemes: tuple[str, ...] = ("http", "https")) -> None:
    require(name, value)
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in schemes and bool(parsed.hostname) and not any(c.isspace() for c in value)
        # MongoDB 副本集可有多个 seed；不使用会对 SRV 做 DNS 查询的 URI 解析器。
        seeds = parsed.netloc.rsplit("@", 1)[-1].split(",")
        if parsed.scheme == "mongodb+srv" and len(seeds) != 1:
            valid = False
        for seed in seeds:
            host = urlsplit("//" + seed)
            port = host.port
            valid = valid and bool(host.hostname) and (port is None or 0 < port <= 65535)
            if parsed.scheme == "mongodb+srv" and port is not None:
                valid = False
        if parsed.scheme not in {"mongodb", "mongodb+srv"} and len(seeds) != 1:
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError(f"{name} 必须为有效的 {'/'.join(schemes)} 地址")


def validate_device(name: str, value: str) -> None:
    if not re.fullmatch(r"cpu|mps|cuda(?::\d+)?", value):
        raise ConfigError(f"{name} 必须为 cpu、mps、cuda 或 cuda:N")


def model_source(local_path: str | None, model_name: str, path_var: str, name_var: str) -> str:
    """只验证本地目录/仓库名，不探测硬件、不下载；相对路径基于项目根目录。"""
    def directory(value: str, name: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.is_dir():
            raise ConfigError(f"{name} 必须指向已存在的本地模型目录")
        return str(path.resolve())

    if local_path:
        return directory(local_path, path_var)
    require(name_var, model_name)
    # 兼容旧变量中的本地路径；明显的路径绝不作为仓库名交给 SDK。
    candidate = Path(model_name).expanduser()
    candidate = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
    if candidate.is_dir():
        return str(candidate.resolve())
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*)?", model_name):
        raise ConfigError(f"{name_var} 必须为模型仓库名；本地路径请配置 {path_var} 并确认目录存在")
    return model_name
