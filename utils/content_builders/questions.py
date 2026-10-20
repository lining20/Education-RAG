"""题目构建器：一道题一条完整记录，答案解析保持关联，缺失项明确标注。"""

import re

from common.errors import OperationError
from common.schemas.milvus_schema import QUESTION_TYPE_LABELS
from processor.import_processor.state import ContentRecord, DataDict
from utils.content_builders.projection import label, plain, stable_record_id
from utils.parsers.image_understanding import image_supplement, image_supplement_text
from utils.content_builders.code_blocks import block_code_text

_TYPE_CODES = {label_: code for code, label_ in QUESTION_TYPE_LABELS.items()}
_OPTION = re.compile(r"^([A-Za-z])[.、．:：]\s*(.*)$")
_FIELDS = {"题型": "question_type", "题干": "stem", "答案": "answer", "解析": "explanation"}


def _parse_options(text: str) -> list[DataDict]:
    options: list[DataDict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _OPTION.match(line)
        if match:
            options.append({"label": match.group(1).upper(), "text": match.group(2).strip()})
        elif options:
            options[-1]["text"] = f"{options[-1]['text']} {line}".strip()
    return options


def _parse_question(blocks) -> DataDict:
    fields: DataDict = {"question_type_name": "", "stem": "", "options": [],
                        "answer": "", "explanation": ""}
    mode = ""
    for block in blocks:
        if block["block_type"] == "code":
            text = block_code_text(block)
            if mode == "options" and fields["options"]:
                fields["options"][-1]["text"] += "\n\n" + text
            else:
                target = mode if mode in {"stem", "answer", "explanation"} else "stem"
                fields[target] += "\n\n" + text
            continue
        name, value = label(block["text"])
        if name in _FIELDS:
            fields[_FIELDS[name] if name != "题型" else "question_type_name"] = value
            mode = _FIELDS[name] if name != "题型" else ""
        elif name == "选项":
            fields["options"].extend(_parse_options(value))
            mode = "options"
        elif mode == "options":
            fields["options"].extend(_parse_options(plain(block["text"])))
        elif mode in {"stem", "answer", "explanation"} and block["block_type"] != "heading":
            fields[mode] += "\n" + block["text"]
    return fields


def build_question_records(blocks, typed, *, task_id: str, document_id: str,
                           source: DataDict, rejected: list | None = None) -> list[ContentRecord]:
    """按题库+原题编码分组构建；缺题干时记拒绝明细并跳过，其他题目继续。"""
    entries = {item["block_id"]: item for item in typed}
    groups: dict[tuple[str, str], list[DataDict]] = {}
    for block in blocks:
        item = entries.get(block["block_id"])
        if not item or item["content_type"] != "question":
            continue
        metadata = item["metadata"]
        code = (metadata.get("question_code") or "").strip()
        if not code:
            # 题库标题、题库编码等分组头不是题目正文。
            continue
        groups.setdefault(((metadata.get("question_bank_id") or "").strip(),
                           (metadata.get("question_bank_name") or "").strip(),
                           code), []).append(block)

    records: list[ContentRecord] = []
    for (bank_id, bank_name, code), group in groups.items():
        fields = _parse_question(group)
        if not fields["stem"].strip():
            error = OperationError("MISSING_QUESTION_STEM", "题目缺少题干，不能建立题目记录")
            if rejected is None:
                raise error
            rejected.append({"block_id": group[0]["block_id"],
                             "code": error.code, "message": error.message})
            continue
        metadata = entries[group[0]["block_id"]]["metadata"]
        raw_type = fields["question_type_name"] or (metadata.get("question_type") or "")
        type_code = _TYPE_CODES.get(raw_type.strip(), "other" if raw_type.strip() else "")
        points = [str(item) for item in (metadata.get("knowledge_points") or [])]
        content_parts = [fields["stem"]]
        if fields["options"]:
            content_parts.append("\n".join(f"{item['label']}. {item['text']}" for item in fields["options"]))
        if points:
            content_parts.append("知识点: " + "、".join(points))
        supplement = image_supplement(group)
        if image_supplement_text(supplement):
            content_parts.append(image_supplement_text(supplement))
        missing = [name for name in ("answer", "explanation") if not fields[name].strip()]
        records.append({
            "record_id": stable_record_id(task_id, document_id, "question", f"question:{bank_id}:{bank_name}:{code}"),
            "task_id": task_id, "document_id": document_id, "content_type": "question",
            "content": "\n".join(content_parts),
            "metadata": {
                "series_id": metadata.get("series_id", ""), "series_name": metadata.get("series_name", ""),
                "course_id": metadata.get("course_id", ""), "course_name": metadata.get("course_name", ""),
                "question_bank_id": bank_id, "question_bank_name": bank_name,
                "question_code": code, "question_type": type_code,
                "knowledge_points": points,
            },
            "payload": {
                "stem": fields["stem"], "options": fields["options"],
                "answer": fields["answer"], "explanation": fields["explanation"],
                "question_type_name": raw_type, "missing": missing,
                "source_locator": dict(group[0].get("source_locator") or {}),
                "source_blocks": group,
                **supplement,
            },
            "source": dict(source),
        })
    return records
