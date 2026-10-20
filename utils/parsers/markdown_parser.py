"""Markdown 规则解析，保留源码行号、代码围栏、表格和两种图片语法。"""

from contextlib import contextmanager
from html.parser import HTMLParser

from markdown_it import MarkdownIt

from common.errors import OperationError
from common.logging.logger import logger
from utils.parsers.common import ParseInput, ParseResult, make_block, validate_result
from utils.parsers.resources import resolve_local_image


class _HTMLImages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag.lower() == "img" and values.get("src"):
            self.images.append((values["src"], values.get("alt", "")))

    def handle_data(self, data):
        self.text.append(data)


def _images(source, tokens, result):
    images = []
    # 只检查图片/HTML Token，不扫描 code_inline 或代码围栏中的示例。
    for index, token in enumerate(tokens):
        references = []
        if token.type == "image":
            references.append((token.attrGet("src"), token.content))
        elif token.type in {"html_inline", "html_block"}:
            html = _HTMLImages()
            html.feed(token.content)
            references.extend(html.images)
        if token.children:
            images.extend(_images(source, token.children, result))
        for reference, alt in references:
            resource_id = resolve_local_image(source, reference, result)
            image = {"inline_index": index, "reference": reference, "alt": alt}
            if resource_id is not None:
                image["resource_id"] = resource_id
            images.append(image)
    return images


@contextmanager
def parse_markdown(source: ParseInput):
    """同步解析；调用方须在此上下文中上传资源，再退出外层 ZIP 包上下文。"""
    path = source.require_format("md")
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise OperationError("INVALID_MD_ENCODING", "Markdown 必须使用 UTF-8 编码") from exc
    lines = content.splitlines(keepends=True)
    tokens = MarkdownIt("commonmark").enable("table").parse(content)
    result = ParseResult()
    headings = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type not in {"heading_open", "paragraph_open", "table_open", "fence", "code_block", "html_block"}:
            index += 1
            continue
        start, end = token.map
        raw = "".join(lines[start:end])
        payload = {"raw_source": raw}
        relevant = [token]
        if token.type in {"heading_open", "paragraph_open"}:
            inline = tokens[index + 1]
            text = inline.content
            relevant = inline.children or []
            kind = "heading" if token.type == "heading_open" else "paragraph"
            if kind == "heading":
                level = int(token.tag[1:])
                headings = (headings + [""] * level)[:level - 1] + [text]
                payload["level"] = level
            elif relevant and all(t.type == "image" or t.type in {"softbreak", "hardbreak"}
                                  or (t.type == "text" and not t.content.strip()) for t in relevant):
                kind = "image_ref"
        elif token.type == "table_open":
            stop = index + 1
            while tokens[stop].type != "table_close":
                stop += 1
            relevant = tokens[index:stop + 1]
            rows = []
            for child in relevant:
                if child.type == "tr_open":
                    rows.append([])
                elif child.type == "inline":
                    rows[-1].append(child.content)
            kind, text = "table", raw
            payload["rows"] = rows
            index = stop
        elif token.type in {"fence", "code_block"}:
            kind, text = "code", raw
            payload.update(language=token.info.strip(), content=token.content, fence=token.markup)
        else:
            html = _HTMLImages()
            html.feed(token.content)
            text = "".join(html.text).strip()
            kind = "image_ref" if html.images and not text else "paragraph"
            if not html.images:
                text = raw
        images = _images(source, relevant, result)
        if images:
            payload["images"] = images
            payload["resource_ids"] = [item["resource_id"] for item in images if "resource_id" in item]
        result.blocks.append(make_block(source, len(result.blocks), kind, text, headings,
                                        {"format": "md", "line_start": start + 1, "line_end": end}, payload))
        index += 1
    validate_result(result)
    logger.info("MD 解析完成：块数={}，资源数={}", len(result.blocks), len(result.resources))
    yield result
