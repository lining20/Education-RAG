"""公共代码边界：格式解析和两次检索文本投影均保留完整代码块。"""
import re
from markdown_it import MarkdownIt


def fenced_code(content: str, language: str = "") -> str:
    """选择比正文中反引号更长的围栏，保留代码缩进和内部空行。"""
    longest = max((len(m.group()) for m in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return fence + language + "\n" + content + ("" if content.endswith("\n") else "\n") + fence


def code_ranges(text: str) -> list[tuple[int, int]]:
    """返回 Markdown 围栏/缩进代码的字符范围；未闭合围栏保护到末尾。"""
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    ranges = []
    for token in MarkdownIt("commonmark").parse(text):
        if token.type in {"fence", "code_block"} and token.map:
            start, end = token.map
            ranges.append((offsets[start], offsets[end]))
    return ranges


def block_code_text(block: dict) -> str:
    """为不同文件格式的 code 块提供统一、不会被二次投影拆散的围栏。"""
    payload = block.get("payload", {})
    text = payload.get("raw_source", block["text"]).rstrip("\r\n")
    if re.match(r"^ {0,3}(`{3,}|~{3,})", text):
        return text
    return fenced_code(payload.get("content", text), payload.get("language", ""))
