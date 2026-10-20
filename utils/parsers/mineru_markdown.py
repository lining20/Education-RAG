"""以 MinerU 返回的 Markdown 为正文；JSON 仅作可选页码索引。"""
from collections import defaultdict, deque
import json
from pathlib import Path

from common.errors import OperationError
from utils.parsers.common import ParseInput, ResourceContext, make_block
from utils.parsers.markdown_parser import parse_markdown


def _select_markdown(source, package):
    documents = [doc for doc in package.documents if doc.format == "md"]
    for name in (Path(source.file_path).stem + ".md", "full.md"):
        matches = [doc for doc in documents if Path(doc.relative_path).name == name]
        if len(matches) == 1:
            return matches[0]
    if len(documents) == 1:
        return documents[0]
    raise OperationError("INVALID_MINERU_MARKDOWN", "MinerU 结果包缺少唯一可确定的 Markdown 正文")


def _page_lookup(package, document, page_count):
    """只读取可明确关联的文本/图片和合法页码，不要求认识每种 JSON 内容类型。"""
    by_text, by_image = defaultdict(deque), defaultdict(deque)
    parent = Path(document.file_path).parent
    candidates = sorted(parent.glob("*content_list.json"))
    if not candidates:
        candidates = sorted(parent.glob("structured_content.json"))
    if len(candidates) != 1:
        return by_text, by_image
    try:
        data = json.loads(candidates[0].read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and isinstance(data.get("pages"), list):
            data = [{**block, "page_idx": block.get("page_idx", page.get("page_idx"))}
                    for page in data["pages"] if isinstance(page, dict)
                    for block in page.get("blocks", []) if isinstance(block, dict)]
        if not isinstance(data, list):
            return by_text, by_image
        for item in data:
            if not isinstance(item, dict):
                continue
            page = item.get("page_idx")
            if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page < page_count:
                continue
            text = item.get("text", item.get("content", item.get("table_body", "")))
            if isinstance(text, str) and text.strip():
                by_text[text.strip()].append(page)
            reference = item.get("img_path")
            if isinstance(reference, str):
                by_image[reference].append(page)
    except (OSError, ValueError, TypeError, AttributeError):
        # 页码索引损坏不阻止读取有效 Markdown；调用方给出定位降级警告。
        pass
    return by_text, by_image


def append_markdown_result(source, package, result, headings, *, part_id,
                           source_format, start_page=0, page_count=None):
    document = _select_markdown(source, package)
    context = ResourceContext(package.resource_root, document.relative_path,
                              lambda ref: package.resolve_resource(document, ref))
    parsed_source = ParseInput(document.file_path, f"{source.document_id}:{part_id}", context, source.stop_event)
    by_text, by_image = _page_lookup(package, document, page_count) if page_count else ({}, {})
    unmapped = False
    with parse_markdown(parsed_source) as parsed:
        for block in parsed.blocks:
            if block["block_type"] == "heading":
                level = block["payload"]["level"]
                headings[:] = (headings + [""] * level)[:level - 1] + [block["text"]]
            original = block["source_locator"]
            locator = {"format": source_format, "parsed_format": "md", "part_id": part_id,
                       "markdown_path": document.relative_path,
                       "markdown_line_start": original["line_start"], "markdown_line_end": original["line_end"]}
            if source_format == "pdf":
                locator.update(part_start_page=start_page + 1, part_page_count=page_count)
                matches = by_text.get(block["text"].strip())
                if not matches:
                    for image in block["payload"].get("images", []):
                        matches = by_image.get(image["reference"])
                        if matches:
                            break
                if matches:
                    local_page = matches.popleft()
                    locator.update(page=start_page + local_page + 1, part_page=local_page + 1)
                else:
                    unmapped = True
            payload = {**block["payload"], "parser": "mineru_vlm_markdown", "part_id": part_id}
            result.blocks.append(make_block(source, len(result.blocks), block["block_type"], block["text"],
                                            headings, locator, payload))
        result.resources.extend(parsed.resources)
        result.warnings.extend(parsed.warnings)
    if unmapped:
        result.warnings.append({"code": "MINERU_PAGE_LOCATOR_UNAVAILABLE",
            "message": "部分 Markdown 内容无法明确关联原 PDF 页码，保留分段范围及 Markdown 行号，不推测页码"})
