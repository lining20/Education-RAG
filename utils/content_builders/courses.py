"""课程介绍构建器：系列与子课程分别建记录，子课程继承已明确的系列元数据。"""

from common.errors import OperationError
from processor.import_processor.state import ContentRecord, DataDict
from utils.content_builders.projection import label, split_items, stable_record_id
from utils.parsers.image_understanding import image_supplement, image_supplement_text
from utils.content_builders.code_blocks import block_code_text


def _locator(block: DataDict) -> DataDict:
    return dict(block.get("source_locator") or {})


def _group_blocks(blocks, typed):
    """按（系列名，子课程名）分组；无系列归属的文件头块直接跳过。"""
    entries = {item["block_id"]: item for item in typed}
    groups: dict[tuple[str, str], list[DataDict]] = {}
    for block in blocks:
        item = entries.get(block["block_id"])
        if not item or item["content_type"] != "course_intro":
            continue
        metadata = item["metadata"]
        series = (metadata.get("series_name") or "").strip()
        if not series:
            continue
        groups.setdefault((series, metadata.get("course_name", "").strip()), []).append(block)
    return groups


def _series_attributes(blocks, metadata) -> DataDict:
    attributes = {
        "series_id": (metadata.get("series_id") or "").strip(),
        "description": "", "category_path": [], "audience": [],
        "learning_goals": [], "grade_levels": [],
    }
    for block in blocks:
        if block["block_type"] == "code":
            continue
        name, value = label(block["text"])
        if name == "系列编码":
            attributes["series_id"] = value
        elif name == "描述":
            attributes["description"] = value
        elif name == "课程分类":
            attributes["category_path"] = split_items(value)
        elif name == "适合人群":
            attributes["audience"] = split_items(value)
        elif name == "学习目标":
            attributes["learning_goals"] = split_items(value)
        elif name == "适合年级":
            attributes["grade_levels"] = split_items(value)
    return attributes


def _course_attributes(blocks) -> DataDict:
    attributes = {"course_id": "", "description": "",
                  "lesson_count": None, "study_hours": None}
    for block in blocks:
        if block["block_type"] == "code":
            continue
        name, value = label(block["text"])
        if name == "编码":
            parts = value.split(",")
            attributes["course_id"] = parts[0].strip()
            for part in parts[1:]:
                key, _, raw = part.partition(":")
                key, raw = key.strip(), raw.strip()
                try:
                    if key == "课时" and raw:
                        attributes["lesson_count"] = int(float(raw))
                    elif key == "学时" and raw:
                        attributes["study_hours"] = float(raw)
                except ValueError:
                    # 样本异常值不臆造数字，保持 None 并交由后续警告处理。
                    pass
        elif name == "描述":
            attributes["description"] = value
    return attributes


def build_course_records(blocks, typed, *, task_id: str, document_id: str,
                         source: DataDict, rejected: list | None = None) -> list[ContentRecord]:
    """构建系列与子课程完整记录。

    传入 rejected 列表时，缺少业务编码的单元记入该列表并跳过；否则直接抛错。
    系列与子课程需要互相引用，因此必须一次传入整篇文档的课程块。
    """
    groups = _group_blocks(blocks, typed)
    if not groups:
        return []

    series_names: dict[str, list[str]] = {}
    for series, course in groups:
        if course and course not in series_names.setdefault(series, []):
            series_names[series].append(course)

    records: list[ContentRecord] = []
    # 先输出系列记录，再输出其子课程，保证列表/详情层级稳定。
    for (series, course), group_blocks in groups.items():
        if course:
            continue
        metadata = next((item["metadata"] for item in typed
                         if item["block_id"] == group_blocks[0]["block_id"]), {})
        attributes = _series_attributes(group_blocks, metadata)
        if not attributes["series_id"]:
            error = OperationError("MISSING_SERIES_CODE", "课程系列缺少系列编码，不能建立课程记录")
            if rejected is None:
                raise error
            rejected.append({"block_id": group_blocks[0]["block_id"],
                             "code": error.code, "message": error.message})
            continue
        children = series_names.get(series, [])
        supplement = image_supplement(group_blocks)
        content_parts = [series]
        if attributes["description"]:
            content_parts.append(attributes["description"])
        if attributes["category_path"]:
            content_parts.append("课程分类: " + " / ".join(attributes["category_path"]))
        if attributes["audience"]:
            content_parts.append("适合人群: " + "、".join(attributes["audience"]))
        if attributes["learning_goals"]:
            content_parts.append("学习目标: " + "、".join(attributes["learning_goals"]))
        if children:
            content_parts.append("包含课程: " + "、".join(children))
        if image_supplement_text(supplement):
            content_parts.append(image_supplement_text(supplement))
        content_parts.extend(block_code_text(b) for b in group_blocks if b["block_type"] == "code")
        records.append({
            "record_id": stable_record_id(task_id, document_id, "course_intro",
                                          f"series:{attributes['series_id']}"),
            "task_id": task_id, "document_id": document_id, "content_type": "course_intro",
            "content": "\n".join(content_parts),
            "metadata": {
                "entity_type": "series", "series_id": attributes["series_id"],
                "series_name": series, "course_id": "", "course_name": "",
                "category_path": attributes["category_path"], "audience": attributes["audience"],
                "learning_goals": attributes["learning_goals"],
                "grade_levels": attributes["grade_levels"], "knowledge_points": [],
            },
            "payload": {
                "entity_type": "series", "description": attributes["description"],
                "category_path": attributes["category_path"], "audience": attributes["audience"],
                "learning_goals": attributes["learning_goals"],
                "grade_levels": attributes["grade_levels"], "courses": children,
                "source_locator": _locator(group_blocks[0]),
                "source_blocks": group_blocks,
                **supplement,
            },
            "source": dict(source),
        })

    for (series, course), group_blocks in groups.items():
        if not course:
            continue
        metadata = next((item["metadata"] for item in typed
                         if item["block_id"] == group_blocks[0]["block_id"]), {})
        attributes = _course_attributes(group_blocks)
        course_id = attributes["course_id"] or (metadata.get("course_id") or "").strip()
        series_id = (metadata.get("series_id") or "").strip()
        if not course_id:
            error = OperationError("MISSING_COURSE_CODE", "子课程缺少课程编码，不能建立课程记录")
            if rejected is None:
                raise error
            rejected.append({"block_id": group_blocks[0]["block_id"],
                             "code": error.code, "message": error.message})
            continue
        if not series_id:
            error = OperationError("MISSING_SERIES_CODE", "子课程缺少系列编码，不能建立课程记录")
            if rejected is None:
                raise error
            rejected.append({"block_id": group_blocks[0]["block_id"],
                             "code": error.code, "message": error.message})
            continue
        content_parts = [course, f"所属系列: {series}"]
        supplement = image_supplement(group_blocks)
        if attributes["description"]:
            content_parts.append(attributes["description"])
        if attributes["lesson_count"] is not None:
            content_parts.append(f"课时: {attributes['lesson_count']}")
        if attributes["study_hours"] is not None:
            content_parts.append(f"学时: {attributes['study_hours']}")
        if image_supplement_text(supplement):
            content_parts.append(image_supplement_text(supplement))
        content_parts.extend(block_code_text(b) for b in group_blocks if b["block_type"] == "code")
        records.append({
            "record_id": stable_record_id(task_id, document_id, "course_intro",
                                          f"course:{course_id}"),
            "task_id": task_id, "document_id": document_id, "content_type": "course_intro",
            "content": "\n".join(content_parts),
            "metadata": {
                "entity_type": "course", "series_id": series_id, "series_name": series,
                "course_id": course_id, "course_name": course,
                "category_path": [], "audience": [], "learning_goals": [],
                "grade_levels": [], "knowledge_points": [],
            },
            "payload": {
                "entity_type": "course", "description": attributes["description"],
                "lesson_count": attributes["lesson_count"], "study_hours": attributes["study_hours"],
                "series_id": series_id, "series_name": series,
                "source_locator": _locator(group_blocks[0]),
                "source_blocks": group_blocks,
                **supplement,
            },
            "source": dict(source),
        })
    return records
