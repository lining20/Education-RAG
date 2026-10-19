"""无环境加载副作用的项目路径定位。"""

import os
from pathlib import Path


def get_path_dir(ps: int = 0) -> Path:
    return Path(__file__).parents[ps]


def get_project_root(identifier: str = ".env") -> Path:
    env_root = os.getenv("PROJECT_ROOT", "").strip()
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("PROJECT_ROOT 必须指向已存在的目录")
        return root
    # 从源码定位，与启动工作目录无关；没有 .env 的测试/部署也能导入。
    for root in Path(__file__).resolve().parents:
        if (root / identifier).is_file() or (root / "pyproject.toml").is_file():
            return root
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = get_project_root()
