"""文档切分：标题分组 → 长块递归切分 → 同章短块合并，保留原始位置。"""
from pathlib import Path
from copy import deepcopy

from langchain_text_splitters import RecursiveCharacterTextSplitter

from common.logging.logger import logger, step_log
from processor.import_processor.state import ContentRecord, DataDict
from utils.content_builders.projection import stable_record_id
from utils.content_builders.code_blocks import code_ranges, block_code_text

DOCUMENT_TYPES = ("document_chunk", "project_material")
CHUNK_MAX_SIZE = 1000
CHUNK_SIZE = 600
CHUNK_OVERLAP = 50
CHUNK_MIN = 400
_CONTEXT_FIELDS = (
    "series_id", "series_name", "course_id", "course_name", "project_id", "project_name",
    "chapter_id", "chapter_name",
)


def _context(entry):
    return (entry["content_type"], *(entry["metadata"].get(key, "") for key in _CONTEXT_FIELDS))


@step_log("step_attach_images_to_nearby_text")
def attach_images_to_nearby_text(blocks, entries):
    """同标题、同业务归属内，将独立原图关联到最近正文；不修改正文或代码。"""
    blocks = deepcopy(blocks)
    attached = 0
    for index, block in enumerate(blocks):
        entry = entries.get(block["block_id"])
        resources = block["payload"].get("resources", [])
        if (block["block_type"] != "image_ref" or not resources or not entry
                or entry["content_type"] not in DOCUMENT_TYPES):
            continue
        candidates = [i for i, other in enumerate(blocks)
                      if other["block_type"] not in {"heading", "image_ref"}
                      and other["text"].strip()
                      and other["heading_path"] == block["heading_path"]
                      and other["block_id"] in entries
                      and _context(entries[other["block_id"]]) == _context(entry)]
        if not candidates:
            continue
        # 距离相同时优先上文，避免把章末配图放到后面的说明中。
        target = blocks[min(candidates, key=lambda i: (abs(i - index), i > index))]
        linked = target["payload"].setdefault("resources", [])
        keys = {resource["object_key"] for resource in linked}
        for resource in resources:
            if resource["object_key"] not in keys:
                linked.append({**resource, "source_locator": dict(block["source_locator"]),
                               "heading_path": list(block["heading_path"])})
                keys.add(resource["object_key"])
        block["payload"]["image_attached_to"] = target["block_id"]
        attached += 1
    logger.info("独立图片块关联完成：已关联正文={}，保持正文与代码原样", attached)
    return blocks


@step_log("step_2_split_document_by_title")
def step_2_split_document_by_title(blocks, entries, file_title) -> list[DataDict]:
    """按解析器给出的完整标题路径组织正文，不把标题本身生成一个碎片。

    代码围栏已由格式解析器识别，无需再用正则扫描代码里的 #。
    代码、表格与邻近说明一起整理；标题前正文并入首个有内容的标题段。
    """
    sections, orphans = [], []
    first_heading = None
    for block in blocks:
        entry = entries.get(block["block_id"])
        if not entry or entry["content_type"] not in DOCUMENT_TYPES:
            continue
        if (block["block_type"] == "image_ref" and block["payload"].get("image_attached_to")
                and not block["payload"].get("image_descriptions")):
            continue  # 原图已关联正文，不再把图片语法单独生成一个碎片。
        heading = list(block["heading_path"])
        if block["block_type"] == "heading":
            if first_heading is None:
                first_heading = heading
        # raw_source 保留 Markdown 列表、表格、代码缩进与代码内部空行。
        text = block.get("payload", {}).get("raw_source", block["text"]).rstrip("\r\n")
        if block["block_type"] == "code":
            text = block_code_text(block)
        parts = []
        if text and block["block_type"] != "heading":
            parts.append({"block": block, "text": text,
                          "normalized_code": block["block_type"] == "code" and
                          text != block.get("payload", {}).get("raw_source", block["text"]).rstrip("\r\n")})
        for description in block["payload"].get("image_descriptions", []):
            if description["text"]:
                parts.append({"block": block,
                    "text": "[图片内容说明·视觉模型生成]\n" + description["text"],
                    "generated": True, "resource_id": description["resource_id"]})
        if not parts:
            continue
        if not heading and not sections:
            orphans.extend((part, entry) for part in parts)
            continue
        if (not sections or sections[-1]["heading"] != heading
                or _context(sections[-1]["entry"]) != _context(entry)):
            sections.append({"heading": heading or [file_title], "entry": entry, "parts": []})
        if orphans:
            sections[-1]["parts"].extend(part for part, _ in orphans)
            orphans = []
        sections[-1]["parts"].extend(parts)
    if orphans:
        sections.append({"heading": first_heading or [file_title], "entry": orphans[0][1],
                         "parts": [part for part, _ in orphans]})
    for section in sections:
        # metadata_recognition 会前置这些归属名称，为最终向量文本预留容量。
        metadata = section["entry"]["metadata"]
        names = [metadata.get(f"{key}_name", "") for key in ("series", "course", "project", "chapter")]
        context = "\n".join(dict.fromkeys(name for name in names if name))
        section["context_size"] = len(context) + 1 if context else 0
        position = 0
        texts = []
        for part in section["parts"]:
            part["start"] = position
            part["end"] = position + len(part["text"])
            position = part["end"] + 2
            texts.append(part["text"])
        section["body"] = "\n\n".join(texts)
    logger.info("按完整标题路径组织正文：标题段={}", len(sections))
    return sections


def _prefix(section):
    title = "_".join(section["heading"])
    # 极长标题始终完整保存在元数据；不让标题本身挤占全部正文预算。
    return title + "\n" if len(title) < CHUNK_MAX_SIZE - section["context_size"] - CHUNK_MIN else ""


def _compact_spans(spans):
    """合并同一原文段的相邻范围，去掉重叠切分引入的重复正文。"""
    result = []
    for span in spans:
        if result and result[-1]["section"] is span["section"] and span["start"] <= result[-1]["end"]:
            result[-1]["end"] = max(result[-1]["end"], span["end"])
        else:
            result.append(dict(span))
    return result


def _render(spans):
    return "\n\n".join(_prefix(s["section"]) + s["section"]["body"][s["start"]:s["end"]]
                         for s in spans)


def _same_chapter(left, right):
    """只合并同父标题或直接父子段，且课程/项目/章节归属必须一致。"""
    a, b = left[-1]["section"], right[0]["section"]
    if _context(a["entry"]) != _context(b["entry"]):
        return False
    x, y = a["heading"], b["heading"]
    return (x == y or (len(x) > 1 and x[:-1] == y[:-1])
            or x == y[:-1] or y == x[:-1])


@step_log("step_3_refine_split_and_merge_chunks")
def step_3_refine_split_and_merge_chunks(sections) -> list[list[DataDict]]:
    """不超过 1000 字的标题段保持完整；长段以 600 为目标、50 为重叠。

    按段落、换行、中文标点递归细分，最终空分隔符处理无标点长文本。
    代码整体保留，可超过普通长度目标；代码内部不细分、不重叠。
    不足 400 字的相邻同章片段，合并后不超过 1000 字时合并。
    400 是合并阈值，不为了凑字数跨越无关的一级标题。
    """
    candidates = []
    for section in sections:
        text = section["body"]
        prefix = _prefix(section)
        context_size = section["context_size"]
        if len(prefix) + len(text) + context_size <= CHUNK_MAX_SIZE:
            candidates.append([{"section": section, "start": 0, "end": len(text)}])
            continue
        target = max(CHUNK_OVERLAP + 1, CHUNK_SIZE - len(prefix) - context_size)
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=target, chunk_overlap=CHUNK_OVERLAP,
            separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
            keep_separator=True, strip_whitespace=False, add_start_index=True,
        )
        # 先隔离代码；仅对代码之间的正文递归切分，重叠也不进入代码内部。
        ranges = code_ranges(text)
        ranges.extend((p["start"], p["end"]) for p in section["parts"]
                      if p["block"]["block_type"] == "code" and not p.get("generated"))
        protected = []
        for start, end in sorted(ranges):
            if protected and start <= protected[-1][1]:
                protected[-1] = (protected[-1][0], max(end, protected[-1][1]))
            else:
                protected.append((start, end))
        cursor = 0
        for code_start, code_end in [*protected, (len(text), len(text))]:
            for document in splitter.create_documents([text[cursor:code_start]]):
                start = cursor + document.metadata["start_index"]
                if document.metadata["start_index"] < 0:
                    raise ValueError("切分结果无法映射回原文")
                candidates.append([{"section": section, "start": start, "end": start + len(document.page_content)}])
            if code_end > code_start:
                candidates.append([{"section": section, "start": code_start, "end": code_end}])
            cursor = code_end
    merged = []
    for candidate in candidates:
        if merged and _same_chapter(merged[-1], candidate):
            short = min(len(_render(merged[-1])), len(_render(candidate))) < CHUNK_MIN
            combined = _compact_spans([*merged[-1], *candidate])
            if short and len(_render(combined)) + combined[0]["section"]["context_size"] <= CHUNK_MAX_SIZE:
                merged[-1] = combined
                continue
        merged.append(candidate)
    logger.info("长块切分与短块合并：细分后={}，合并后={}，目标={}，上限={}，重叠={}",
                len(candidates), len(merged), CHUNK_SIZE, CHUNK_MAX_SIZE, CHUNK_OVERLAP)
    return merged


def _source_segments(spans):
    """每个片段保留原块 ID、标题、字符范围与真实页码/行号，合并后仍可追溯。"""
    segments = []
    for span in spans:
        for part in span["section"]["parts"]:
            start = max(span["start"], part["start"]) - part["start"]
            end = min(span["end"], part["end"]) - part["start"]
            if start >= end:
                continue
            block = part["block"]
            locator = dict(block.get("source_locator") or {})
            if not part.get("generated") and not part.get("normalized_code") and locator.get("format") == "md" and "line_start" in locator:
                locator["line_start"] += part["text"][:start].count("\n")
                locator["line_end"] = locator["line_start"] + part["text"][start:end].count("\n")
                if part["text"][start:end].endswith("\n"):
                    locator["line_end"] -= 1
            segment = {"block_id": block["block_id"], "block_type": block["block_type"],
                             "heading_path": list(block["heading_path"]), "source_locator": locator,
                             "char_start": start, "char_end": end}
            if part.get("generated"):
                # 字符范围属于生成说明，行号/页码仅定位原图，不冒充原文文本位置。
                segment.update(content_origin="vision_model", resource_id=part["resource_id"],
                               char_range_basis="image_description")
            elif part.get("normalized_code"):
                segment["char_range_basis"] = "normalized_code_with_fence"
            segments.append(segment)
    return segments


def _common_heading(paths):
    common = list(paths[0])
    for path in paths[1:]:
        count = 0
        while count < min(len(common), len(path)) and common[count] == path[count]:
            count += 1
        common = common[:count]
    return common


def build_document_chunks(blocks, typed, *, task_id: str, document_id: str,
                          source: DataDict) -> list[ContentRecord]:
    """复用统一内容块，MD/PDF/DOCX 共用标题分组与长度策略。"""
    entries = {item["block_id"]: item for item in typed}
    blocks = attach_images_to_nearby_text(blocks, entries)
    file_title = Path(source.get("file_name") or document_id).stem
    sections = step_2_split_document_by_title(blocks, entries, file_title)
    chunks = step_3_refine_split_and_merge_chunks(sections)
    records = []
    by_id = {block["block_id"]: block for block in blocks}
    for ordinal, spans in enumerate(chunks):
        segments = _source_segments(spans)
        if not segments:
            continue
        paths = [span["section"]["heading"] for span in spans]
        heading = _common_heading(paths)
        locator = dict(segments[0]["source_locator"])
        locator["segments"] = segments
        if locator.get("format") == "md":
            locator["line_end"] = max(s["source_locator"].get("line_end", 0) for s in segments)
        if locator.get("format") == "pdf" and "page" in locator:
            locator["page_end"] = max(s["source_locator"].get("page", 0) for s in segments)
        entry = spans[0]["section"]["entry"]
        metadata = {key: entry["metadata"].get(key, "") for key in _CONTEXT_FIELDS}
        metadata["knowledge_points"] = list(entry["metadata"].get("knowledge_points") or [])
        resources = {}
        descriptions = {}
        for segment in segments:
            for resource in by_id[segment["block_id"]].get("payload", {}).get("resources", []):
                resources[resource["object_key"]] = resource
            for description in by_id[segment["block_id"]]["payload"].get("image_descriptions", []):
                descriptions[description["resource_id"]] = description
        title = "_".join(heading)
        records.append({
            "record_id": stable_record_id(task_id, document_id, entry["content_type"],
                f"document:{ordinal}:{segments[0]['block_id']}:{segments[0]['char_start']}"),
            "task_id": task_id, "document_id": document_id, "content_type": entry["content_type"],
            "content": _render(spans), "metadata": metadata,
            "payload": {"title": title, "parent_title": "_".join(heading[:-1]),
                "heading_path": heading, "heading_paths": paths, "content_type": entry["content_type"],
                "source_locator": locator, "source_segments": segments, "resources": list(resources.values()),
                "image_descriptions": list(descriptions.values())},
            "source": dict(source),
        })
    return records
