"""PDF 物理分段及 MinerU v4 本地文件上传适配，不执行向量文本切分。

契约依据：mineru.net/apiManage/docs 的 file-urls/batch、extract-results/batch
以及标准 ZIP 的 *_content_list.json/full.md/images。未知 JSON 结构明确失败。
"""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import fitz
import requests

from common.config.mineru_config import MineruConfig
from common.errors import OperationError
from common.logging.logger import logger
from utils.parsers.common import ParseInput, ParseResult, validate_result
from utils.zip_preprocessor import prepare_zip_package, ZipLimits


@dataclass(frozen=True)
class PdfLimits:
    max_pages: int = 200
    max_file_bytes: int = 200_000_000  # 云端 200 MB，按十进制字节设置。
    result_zip_limits: ZipLimits = ZipLimits()

    def __post_init__(self):
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
               for value in (self.max_pages, self.max_file_bytes)):
            raise ValueError("PDF 页数和大小限制必须为正整数")
        if self.max_pages > 200 or self.max_file_bytes > 200_000_000:
            raise ValueError("PDF 分段限制不能超过 MinerU v4 的 200 页/200 MB")


@dataclass(frozen=True)
class PdfPart:
    file_path: str
    start_page: int  # 原文件零基起始页。
    page_count: int

    @property
    def part_id(self):
        return f"p{self.start_page + 1:06d}-{self.start_page + self.page_count:06d}"


def split_pdf(file_path: str, work_dir: Path, *, limits: PdfLimits) -> list[PdfPart]:
    """按连续页物理生成 PDF，并检查实际大小；超大段二分，超大单页明确失败。"""
    parts = []
    with fitz.open(file_path) as original:
        if not original.is_pdf or original.needs_pass or original.page_count == 0:
            raise OperationError("INVALID_PDF", "PDF 无有效页面或需要密码")

        def write_range(start, stop):
            part = PdfPart(str(work_dir / f"p{start + 1:06d}-{stop:06d}.pdf"), start, stop - start)
            with fitz.open() as output:
                output.insert_pdf(original, from_page=start, to_page=stop - 1)
                output.save(part.file_path, garbage=4, deflate=True)
            if Path(part.file_path).stat().st_size <= limits.max_file_bytes:
                parts.append(part)
                return
            Path(part.file_path).unlink()  # 只删除本函数在私有工作目录创建的分段文件。
            if stop - start == 1:
                raise OperationError("PDF_PAGE_TOO_LARGE", "PDF 单页仍超过云端大小限制，不能自动降质上传")
            middle = (start + stop) // 2
            write_range(start, middle)
            write_range(middle, stop)

        for start in range(0, original.page_count, limits.max_pages):
            write_range(start, min(start + limits.max_pages, original.page_count))
    return parts


from utils.parsers.mineru_transport import upload_and_download
from utils.parsers.mineru_markdown import append_markdown_result


@contextmanager
def parse_pdf(source: ParseInput, *, config: MineruConfig, limits: PdfLimits = PdfLimits()):
    """PDF 物理分段 → MinerU VLM 转 MD → 读取 MD 正文，图片保留至公共节点结束。"""
    path = source.require_format("pdf")
    config.validate()
    if not urlsplit(config.base_url).path.rstrip("/").endswith("/api/v4"):
        raise OperationError("INVALID_MINERU_ENDPOINT", "MinerU 需要标准 /api/v4 地址")
    with TemporaryDirectory(prefix="parse_pdf_") as directory, ExitStack() as packages:
        work_dir = Path(directory)
        result = ParseResult()
        headings = []
        try:
            parts = split_pdf(str(path), work_dir, limits=limits)
            for part in parts:
                archive_path = upload_and_download(part, work_dir, config, limits, stop_event=source.stop_event)
                package = packages.enter_context(prepare_zip_package(archive_path, limits=limits.result_zip_limits))
                append_markdown_result(source, package, result, headings, part_id=part.part_id,
                                       source_format="pdf", start_page=part.start_page, page_count=part.page_count)
            validate_result(result)
        except requests.RequestException as exc:
            raise OperationError("MINERU_NETWORK_ERROR", "MinerU 网络请求失败", retryable=True) from exc
        logger.info("PDF 转 MD 完成：分段数={}，块数={}，资源数={}", len(parts), len(result.blocks), len(result.resources))
        yield result
