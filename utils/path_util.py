"""无环境加载副作用的项目路径定位。"""

import os
from pathlib import Path
import stat

from common.errors import OperationError


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


def validate_import_file(value: str, *, max_file_bytes: int) -> tuple[str, str, int]:
    """只读检查绝对临时路径、后缀、普通文件/大小和可读性，返回路径/格式/字节数。

    不解析正文或上传文件。PDF/DOCX 结构、MD 编码由 parse_file 验证；
    扩展名通过不代表内容已解析成功。调用方须在线程中执行此同步 I/O。
    """
    if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes <= 0:
        raise ValueError("max_file_bytes 必须为正整数")
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise OperationError("INVALID_FILE_PATH", "file_path 必须为非空且不含空字符的路径")
    path = Path(value)
    if not path.is_absolute():
        raise OperationError("INVALID_FILE_PATH", "file_path 必须是 Service 提供的绝对临时路径")
    suffix = path.suffix.lower()
    if suffix not in {".md", ".docx", ".pdf"}:
        raise OperationError("UNSUPPORTED_FILE_FORMAT", "仅支持 md、docx、pdf；旧版 doc 尚未适配")
    try:
        path = path.resolve(strict=True)
        if path.suffix.lower() != suffix:
            raise OperationError("FILE_FORMAT_MISMATCH", "路径解析前后的文件扩展名不一致")
        if not stat.S_ISREG(path.stat().st_mode):
            raise OperationError("INVALID_FILE_PATH", "file_path 必须指向普通文件")
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise OperationError("INVALID_FILE_PATH", "file_path 必须指向普通文件")
            if info.st_size == 0:
                raise OperationError("EMPTY_FILE", "待导入文件为空")
            if info.st_size > max_file_bytes:
                raise OperationError("FILE_TOO_LARGE", "待导入文件超过本次导入大小限制")
            source.read(1)
    except FileNotFoundError as exc:
        raise OperationError("FILE_NOT_FOUND", "待导入文件不存在") from exc
    except OSError as exc:
        raise OperationError("FILE_UNREADABLE", "待导入文件不可读取") from exc
    return str(path), suffix[1:], info.st_size
