"""ZIP 上传包预处理；只解压并列出文档，不解析正文或创建导入任务。

调用方持有 prepare_zip_package 上下文，等全部文档及资源读取结束后退出。
同步 ZIP/文件 I/O 应由未来异步调用方安排在线程执行，不能提前关闭上下文。
"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import stat
from tempfile import TemporaryDirectory
from typing import Iterator
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile, ZipInfo

from common.errors import OperationError
from common.logging.logger import logger


@dataclass(frozen=True)
class ZipLimits:
    """限制成员数量（含目录）、单文件及全部文件的解压字节数。"""

    max_members: int = 1_000
    max_file_bytes: int = 100 * 1024 * 1024
    max_total_bytes: int = 500 * 1024 * 1024

    def __post_init__(self):
        for value in (self.max_members, self.max_file_bytes, self.max_total_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("ZIP 数量和大小限制必须为正整数")


@dataclass(frozen=True)
class PackageDocument:
    file_path: str  # 解压后的绝对路径，只在包上下文有效期间使用。
    relative_path: str  # 相对于 resource_root 的包内路径，统一使用 /。
    format: str


@dataclass(frozen=True)
class PreparedPackage:
    resource_root: str
    documents: list[PackageDocument]
    warnings: list[dict[str, str]]

    def resolve_resource(self, document: PackageDocument, reference: str) -> str:
        """从各文档目录解析本地资源，允许包内 ../，禁止访问包外或外链。

        只返回存在的普通文件路径；不解析正文、不下载或上传资源。
        URL 编码的路径会解码，查询和片段不作为本地文件名的一部分。
        """
        root = Path(self.resource_root).resolve(strict=True)
        document_path = Path(document.file_path).resolve(strict=True)
        if (not document_path.is_relative_to(root)
                or document_path.relative_to(root).as_posix() != document.relative_path):
            raise OperationError("ZIP_UNSAFE_PATH", "文档不属于当前解压包")
        if not isinstance(reference, str) or not reference or "\x00" in reference:
            raise OperationError("ZIP_UNSAFE_PATH", "资源引用必须为有效的包内相对路径")
        parsed = urlsplit(reference)
        path = unquote(parsed.path).replace("\\", "/")
        if (parsed.scheme or parsed.netloc or not path or "\x00" in path or ":" in path
                or PureWindowsPath(path).drive or path.startswith("/")):
            raise OperationError("ZIP_UNSAFE_PATH", "资源引用必须为包内相对路径，不支持外链或绝对路径")
        target = (document_path.parent / path).resolve()
        if not target.is_relative_to(root):
            raise OperationError("ZIP_UNSAFE_PATH", "资源引用不能越过 resource_root")
        if not target.is_file():
            raise OperationError("ZIP_RESOURCE_NOT_FOUND", "包内引用的资源文件不存在")
        return str(target)


def _member_parts(info: ZipInfo) -> tuple[tuple[str, ...], bool]:
    """拒绝路径穿越及 Windows 文件名别名，避免落盘时改变或覆盖其他路径。"""
    name = info.orig_filename.replace("\\", "/")
    parts = tuple(name.removesuffix("/").split("/"))
    invalid_chars = '<>:"|?*'
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10))
    if (name.startswith("/") or PureWindowsPath(name).drive
            or any(not part or part in {".", ".."} or part.endswith((".", " "))
                   or any(ord(char) < 32 or char in invalid_chars for char in part)
                   or part.split(".")[0].upper() in reserved for part in parts)):
        raise OperationError("ZIP_UNSAFE_PATH", "ZIP 包含越界或不安全的成员路径")
    kind = stat.S_IFMT(info.external_attr >> 16)
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
        raise OperationError("ZIP_UNSUPPORTED_ENTRY", "ZIP 不支持链接、特殊文件或加密成员")
    is_dir = name.endswith("/") or kind == stat.S_IFDIR
    if is_dir and info.file_size:
        raise OperationError("ZIP_UNSUPPORTED_ENTRY", "ZIP 目录成员不能携带文件内容")
    return parts, is_dir


def _inspect_members(archive: ZipFile, root: Path, limits: ZipLimits) -> list[tuple[ZipInfo, Path, bool]]:
    """解压前检查全部成员、声明大小和路径冲突；不使用 extractall。"""
    members = archive.infolist()
    if len(members) > limits.max_members:
        raise OperationError("ZIP_TOO_MANY_MEMBERS", "ZIP 成员数量超过限制")
    entries = []
    declared_paths = set()
    tree = {}
    declared_total = 0
    for info in members:
        parts, is_dir = _member_parts(info)
        key = tuple(part.casefold() for part in parts)
        if key in declared_paths:
            raise OperationError("ZIP_PATH_CONFLICT", "ZIP 包含重复或大小写冲突的路径")
        declared_paths.add(key)
        # 同时检查隐含父目录，禁止文件/目录冲突和父目录的大小写别名。
        for depth in range(1, len(parts) + 1):
            expected = (parts[:depth], depth < len(parts) or is_dir)
            previous = tree.setdefault(key[:depth], expected)
            if previous != expected:
                raise OperationError("ZIP_PATH_CONFLICT", "ZIP 路径与其他文件或目录冲突")
        if info.file_size > limits.max_file_bytes:
            raise OperationError("ZIP_FILE_TOO_LARGE", "ZIP 单文件解压大小超过限制")
        declared_total += info.file_size
        if declared_total > limits.max_total_bytes:
            raise OperationError("ZIP_TOTAL_TOO_LARGE", "ZIP 总解压大小超过限制")
        target = root.joinpath(*parts).resolve()
        if not target.is_relative_to(root):
            raise OperationError("ZIP_UNSAFE_PATH", "ZIP 成员不能越过临时解压目录")
        entries.append((info, target, is_dir))
    return entries


def _extract_members(archive: ZipFile, entries: list[tuple[ZipInfo, Path, bool]], limits: ZipLimits) -> int:
    """分块解压；在每次写出前检查实际单文件及累计字节数，不只信任 ZIP 声明。"""
    total_written = 0
    for info, target, is_dir in entries:
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        file_written = 0
        with archive.open(info) as source, target.open("xb") as output:
            while block := source.read(64 * 1024):
                if file_written + len(block) > limits.max_file_bytes:
                    raise OperationError("ZIP_FILE_TOO_LARGE", "ZIP 单文件实际解压大小超过限制")
                if total_written + len(block) > limits.max_total_bytes:
                    raise OperationError("ZIP_TOTAL_TOO_LARGE", "ZIP 实际总解压大小超过限制")
                output.write(block)
                file_written += len(block)
                total_written += len(block)
    return total_written


@contextmanager
def prepare_zip_package(zip_path: str | Path, *, limits: ZipLimits) -> Iterator[PreparedPackage]:
    """保留包目录结构，返回文档清单、资源根目录和警告；退出即清理临时目录。

    只识别 MD/PDF/DOCX 文档，其他普通文件原样保留供资源解析使用。
    无支持文档或校验/解压失败直接抛错并清理，不删除调用方传入的 ZIP。
    调用方必须等全部文档任务及失败/取消收尾结束后再退出该上下文。
    """
    if not isinstance(limits, ZipLimits):
        raise TypeError("limits 必须为 ZipLimits")
    logger.info("ZIP 预处理开始")
    with TemporaryDirectory(prefix="import_zip_") as directory:
        root = Path(directory).resolve()
        try:
            with ZipFile(zip_path) as archive:
                entries = _inspect_members(archive, root, limits)
                documents = [
                    PackageDocument(str(target), target.relative_to(root).as_posix(), target.suffix.lower()[1:])
                    for _, target, is_dir in entries
                    if not is_dir and target.suffix.lower() in {".md", ".pdf", ".docx"}
                ]
                documents.sort(key=lambda document: document.relative_path)
                if not documents:
                    raise OperationError("ZIP_NO_DOCUMENTS", "ZIP 中没有支持的 MD/PDF/DOCX 文档")
                total_written = _extract_members(archive, entries, limits)
        except Exception as exc:
            logger.warning("ZIP 预处理失败，临时目录将清理，code={}", getattr(exc, "code", "ZIP_PREPROCESS_FAILED"))
            raise
        logger.info("ZIP 预处理完成：文档数={}，文件数={}，实际解压字节数={}",
                    len(documents), sum(not is_dir for _, _, is_dir in entries), total_written)
        # 所有文件共享一个目录树；不在单篇文档处理结束时删除共享图片。
        yield PreparedPackage(str(root), documents, [])
