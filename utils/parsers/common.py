"""解析器的轻量合同与必要校验；复用已有 ParsedBlock，不另造 State。"""

from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Callable
from uuid import NAMESPACE_URL, uuid5

from common.errors import OperationError
from processor.import_processor.state import DataDict, ParsedBlock


@dataclass(frozen=True)
class ResourceContext:
    """每篇文档独立的只读上下文；resolve_local 绑定包的公开资源接口。

    不持有清理句柄，不修改包文件；包生命周期由调用方管理。
    """

    resource_root: str
    document_relative_path: str
    resolve_local: Callable[[str], str]


@dataclass(frozen=True)
class ParseInput:
    file_path: str
    document_id: str
    resource_context: ResourceContext | None = None
    stop_event: object | None = None  # 同步 PDF 轮询在请求间检查服务关闭信号。

    def require_format(self, expected: str) -> Path:
        path = Path(self.file_path)
        if path.suffix.lower() != f".{expected}":
            raise OperationError("UNSUPPORTED_PARSE_FORMAT", "解析器与文件格式不匹配，旧 DOC 不受支持")
        if not path.is_absolute() or not path.is_file() or not self.document_id:
            raise OperationError("INVALID_PARSE_INPUT", "解析需要已有文件的绝对路径和文档标识")
        return path


@dataclass(frozen=True)
class ResourceAsset:
    resource_id: str
    file_path: str  # 运行时路径，只在解析上下文内使用，不写入 State。
    original_reference: str
    media_type: str


@dataclass
class ParseResult:
    blocks: list[ParsedBlock] = field(default_factory=list)
    resources: list[ResourceAsset] = field(default_factory=list)
    warnings: list[DataDict] = field(default_factory=list)


Parser = Callable[[ParseInput], AbstractContextManager[ParseResult]]


class ParseSession:
    """调用方持有的单文档解析上下文；临时资源不放进 State。"""
    def __init__(self):
        self._stack = ExitStack()
        self.result: ParseResult | None = None

    def open(self, parser: Parser, source: ParseInput) -> ParseResult:
        if self.result is not None:
            raise OperationError("PARSE_SESSION_CONFLICT", "不能重复打开解析上下文")
        self.result = self._stack.enter_context(parser(source))
        validate_result(self.result)
        return self.result

    def close(self):
        try:
            self._stack.close()
        finally:
            self.result = None


def make_block(source: ParseInput, ordinal: int, block_type: str, text: str,
               heading_path: list[str], locator: DataDict, payload: DataDict | None = None) -> ParsedBlock:
    """同文档、位置及块序生成稳定 ID；位置和 payload 仅含可序列化数据。"""
    locator = dict(locator)
    if source.resource_context is not None:
        locator["document_relative_path"] = source.resource_context.document_relative_path
    identity = json.dumps([source.document_id, ordinal, locator, block_type], sort_keys=True, ensure_ascii=False)
    return {
        "block_id": str(uuid5(NAMESPACE_URL, identity)), "text": text, "block_type": block_type,
        "heading_path": [title for title in heading_path if title], "source_locator": locator, "payload": dict(payload or {}),
    }


def make_asset(source: ParseInput, identity: str, file_path: str,
               reference: str, media_type: str) -> ResourceAsset:
    return ResourceAsset(str(uuid5(NAMESPACE_URL, f"{source.document_id}:{identity}")),
                         file_path, reference, media_type)


def validate_result(result: ParseResult, *, require_content: bool = False) -> None:
    """拒绝空正文、重复块 ID、缺失资源关联和不可序列化的结果。"""
    ids = set()
    for block in result.blocks:
        if (set(block) != set(ParsedBlock.__annotations__)
                or block["block_type"] not in {"heading", "paragraph", "table", "code", "image_ref"}
                or not isinstance(block["text"], str) or not block["block_id"]
                or block["block_id"] in ids):
            raise OperationError("INVALID_PARSE_RESULT", "解析块结构或标识无效")
        ids.add(block["block_id"])
    has_text = any(block["block_type"] in {"paragraph", "table", "code"} and block["text"].strip()
                   for block in result.blocks)
    has_description = any(item.get("text", "").strip() for block in result.blocks
                          for item in block["payload"].get("image_descriptions", []))
    pending_images = any(block["payload"].get("resource_ids") for block in result.blocks)
    if not (has_text or has_description or (pending_images and not require_content)):
        raise OperationError("EMPTY_PARSE_RESULT", "文档没有有效正文或图片识别内容")
    resources = {asset.resource_id for asset in result.resources}
    if any(resource_id not in resources for block in result.blocks
           for resource_id in block["payload"].get("resource_ids", [])):
        raise OperationError("MISSING_PARSE_RESOURCE", "解析块引用了缺失的资源")
    try:
        json.dumps([result.blocks, result.warnings], ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise OperationError("INVALID_PARSE_RESULT", "解析块和警告必须可序列化") from exc
