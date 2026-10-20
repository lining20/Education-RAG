"""按前端类型读取教育资料中的明确标签，不调用模型分类。"""
import re
from common.errors import OperationError
from processor.import_processor.state import DataDict, ImportState
from utils.content_builders.projection import label as _label, plain as _plain, split_items as _split_items

_UPLOAD_LABELS = (
    "series_id", "series_name", "course_id", "course_name", "project_id", "project_name",
    "chapter_id", "chapter_name", "question_bank_id", "question_bank_name",
)
def _level(block: DataDict) -> int:
    level = block.get("payload", {}).get("level", 0)
    return level if isinstance(level, int) and not isinstance(level, bool) else 0


def _empty_series() -> DataDict:
    return {"series_id": "", "series_name": "", "description": "",
            "category_path": [], "audience": [], "learning_goals": [], "grade_levels": []}


def _empty_course() -> DataDict:
    return {"course_id": "", "course_name": "", "description": "",
            "lesson_count": None, "study_hours": None}


def _metadata(series=None, course=None, **extra) -> DataDict:
    """生成固定键集合的块级元数据快照；未知值保持空串/空数组。"""
    series = series or {}
    course = course or {}
    data = {
        "series_id": series.get("series_id", ""), "series_name": series.get("series_name", ""),
        "course_id": course.get("course_id", ""), "course_name": course.get("course_name", ""),
        "project_id": "", "project_name": "", "chapter_id": "", "chapter_name": "",
        "question_bank_id": "", "question_bank_name": "", "question_code": "",
        "question_type": "", "knowledge_points": [],
    }
    data.update(extra)
    return data


def _parse_course_code(value: str) -> tuple[str, int | None, float | None]:
    """解析“编码: xxx, 课时: 8, 学时: 16.00”，缺失项保留 None，不用 0 代替。"""
    code = value.split(",", 1)[0].strip()
    lesson = re.search(r"课时[:：]\s*(\d+)", value)
    hours = re.search(r"学时[:：]\s*([0-9]+(?:\.[0-9]+)?)", value)
    return code, int(lesson.group(1)) if lesson else None, float(hours.group(1)) if hours else None


def _type_course_blocks(blocks: list[DataDict]) -> list[DataDict]:
    """课程介绍：二级标题=系列，其下“### 课程”条目=子课程，属性块归当前层级。"""
    entries = []
    series, course = _empty_series(), _empty_course()
    in_course_section = False
    for block in blocks:
        text = _plain(block["text"]).strip()
        if block["block_type"] == "heading":
            level = _level(block)
            if level == 2:
                series, course = _empty_series(), _empty_course()
                series["series_name"] = text
                in_course_section = False
            elif level == 3:
                in_course_section = text == "课程"
                if in_course_section:
                    course = _empty_course()
        elif block["block_type"] != "code":
            name, value = _label(block["text"])
            if name == "系列编码":
                series["series_id"] = value
            elif name == "课程分类":
                series["category_path"] = _split_items(value)
            elif name == "适合人群":
                series["audience"] = _split_items(value)
            elif name == "学习目标":
                series["learning_goals"] = _split_items(value)
            elif name == "适合年级":
                series["grade_levels"] = _split_items(value)
            elif name == "描述":
                if course["course_name"]:
                    course["description"] = value
                else:
                    series["description"] = value
            elif name == "编码":
                code, lesson, hours = _parse_course_code(value)
                course["course_id"], course["lesson_count"], course["study_hours"] = code, lesson, hours
            elif in_course_section and name is None and text and block["block_type"] != "image_ref":
                # “课程”小节内没有标签的短段落就是下一门子课程名称。
                course = _empty_course()
                course["course_name"] = text
        entries.append({"block_id": block["block_id"], "content_type": "course_intro",
                        "origin": "structure", "metadata": _metadata(series, course)})
    return entries


def _type_question_blocks(blocks: list[DataDict]) -> list[DataDict]:
    """题目资料：二级标题=题库，三级标题=原题编码，固定标签块归当前题。"""
    entries = []
    bank, code, question_type = {"question_bank_id": "", "question_bank_name": ""}, "", ""
    for block in blocks:
        text = _plain(block["text"]).strip()
        if block["block_type"] == "heading":
            level = _level(block)
            if level == 2:
                bank = {"question_bank_id": "", "question_bank_name": text}
                code, question_type = "", ""
            elif level == 3:
                code, question_type = text, ""
        elif block["block_type"] != "code":
            name, value = _label(block["text"])
            if name == "题库编码":
                bank["question_bank_id"] = value
            elif name == "题型":
                question_type = value
        entries.append({
            "block_id": block["block_id"], "content_type": "question", "origin": "structure",
            "metadata": _metadata(**bank, question_code=code, question_type=question_type),
        })
    return entries


def _type_document_blocks(state: ImportState, file_type: str) -> list[DataDict]:
    """讲义/项目资料：内容类型沿用文件级判断，归属只取上传参数。"""
    upload = state.get("upload_metadata") or {}
    extra = {key: upload[key] for key in _UPLOAD_LABELS if isinstance(upload.get(key), str)}
    extra.setdefault("knowledge_points", [])
    origin = "upload" if extra else "structure"
    return [{"block_id": block["block_id"], "content_type": file_type,
             "origin": origin, "metadata": _metadata(**extra)}
            for block in state["parsed_blocks"]]




def annotate_blocks(state: ImportState) -> list[DataDict]:
    """内容类型只取上传参数；结构规则只负责读课程、题库及题号标签。"""
    upload = state["upload_metadata"]
    kind = upload.get("content_type")
    if kind == "course_intro":
        entries = _type_course_blocks(state["parsed_blocks"])
    elif kind == "question":
        entries = _type_question_blocks(state["parsed_blocks"])
    elif kind in {"document_chunk", "project_material"}:
        entries = _type_document_blocks(state, kind)
    else:
        raise OperationError("INVALID_UPLOAD_METADATA", "请先选择内容类型")
    for entry in entries:
        entry["origin"] = "upload"
        for key in _UPLOAD_LABELS:
            if upload.get(key):
                entry["metadata"][key] = upload[key]
        entry["metadata"]["knowledge_points"] = list(upload.get("knowledge_points") or [])
    return entries


def validate_content_structure(blocks, upload_metadata) -> None:
    """视觉调用前复用业务构建规则检查类型匹配，不自动改变用户选择。"""
    kind = upload_metadata.get("content_type")
    if kind not in {"course_intro", "question"}:
        return
    from utils.content_builders.courses import build_course_records
    from utils.content_builders.questions import build_question_records
    entries = annotate_blocks({"parsed_blocks": blocks, "upload_metadata": upload_metadata})
    rejected = []
    builder = build_course_records if kind == "course_intro" else build_question_records
    records = builder(blocks, entries, task_id="preflight", document_id="preflight",
                      source={}, rejected=rejected)
    if not records:
        reason = ("课程介绍用于带系列编码、课程编码的课程目录或简介" if kind == "course_intro"
                  else "题目用于含题号、题干等明确结构的题库")
        raise OperationError("CONTENT_TYPE_MISMATCH", reason + "；当前资料无法建立对应记录，教程、课件或操作讲义请选择“文档片段”后重新上传")
