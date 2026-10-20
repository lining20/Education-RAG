"""按格式装配解析器，供依赖容器注入；只装配，不解析正文。

本模块不做文件 I/O、不读取环境变量、不创建客户端或模型。适配器按需导入，
导入本模块不会加载 PyMuPDF 或请求库。PDF 缺少 MinerU 配置时
在真正调用该解析器时才报错，MD/Word 不需要 MinerU 凭据。
"""

from contextlib import contextmanager

from common.errors import OperationError
from utils.parsers.common import Parser, ParseInput

# 与 state.SourceFile.format 保持一致；键即文件后缀（不含点）。
SUPPORTED_FORMATS: tuple[str, ...] = ("md", "docx", "pdf")


def _pdf_parser(*, mineru_config: object | None, pdf_limits: object | None) -> Parser:
    """把 MinerU 配置和分段限制绑定到统一 Parser 签名，调用时才导入适配器。"""

    @contextmanager
    def parse_mineru_document(source: ParseInput):
        from utils.parsers.pdf_parser import PdfLimits, parse_pdf

        if mineru_config is None:
            raise OperationError("MISSING_PARSER_DEPENDENCY", "PDF 解析需要注入 MinerU 配置")
        with parse_pdf(source, config=mineru_config, limits=pdf_limits or PdfLimits()) as result:
            yield result

    return parse_mineru_document


def build_document_parsers(
    *, mineru_config: object | None = None, pdf_limits: object | None = None,
) -> dict[str, Parser]:
    """返回 md/docx/pdf 三个解析器；MD/Word 本地解析，PDF 使用 MinerU 云端转换链。

    由 Service 构图前调用一次，结果通过 ImportDependencies.document_parsers 注入；
    节点不自行装配，也不读取配置。未知格式键不会被加入映射。
    """
    from utils.parsers.markdown_parser import parse_markdown
    from utils.parsers.docx_parser import parse_docx

    parsers: dict[str, Parser] = {
        "md": parse_markdown,
        "docx": parse_docx,
        "pdf": _pdf_parser(mineru_config=mineru_config, pdf_limits=pdf_limits),
    }
    if tuple(parsers) != SUPPORTED_FORMATS:
        raise RuntimeError("解析器映射与支持格式不一致")
    return parsers
