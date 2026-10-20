"""复用 Markdown 解析器渲染回答；原图仍由核验过的引用单独展示。"""
from markdown_it import MarkdownIt


_markdown = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")


def render_answer(answer: str) -> str:
    # 禁用原始 HTML 和 Markdown 图片，避免模型插入页面内容或任意图片请求。
    return _markdown.render(answer.split("【图片】", 1)[0].rstrip())
