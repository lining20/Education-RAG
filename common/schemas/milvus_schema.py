"""教育知识库的三套 Milvus Schema；本模块不连接数据库或创建集合。"""

from pymilvus import CollectionSchema, DataType, MilvusClient

DEFAULT_DENSE_DIM = 1024
COURSE_ENTITY_TYPES = ("series", "course")
DOCUMENT_CONTENT_TYPES = ("document_chunk", "project_material")
QUESTION_TYPE_LABELS = {
    "single_choice": "单选题",
    "multiple_choice": "多选题",
    "true_false": "判断题",
    "fill_blank": "填空题",
    "short_answer": "简答题",
    "programming": "编程题",
    "calculation": "计算题",
    "proof": "证明题",
    "formula_derivation": "公式推导题",
    "case_analysis": "案例分析题",
    "material_analysis": "材料分析题",
    "scenario_analysis": "情境分析题",
    "data_analysis": "数据分析题",
    "translation": "翻译题",
    "cloze": "完形填空题",
    "reading_comprehension": "阅读理解题",
    "other": "其他",
}


def _add_strings(schema, fields, *, optional=False):
    for name, size, description in fields:
        options = {"default_value": ""} if optional else {}
        schema.add_field(
            field_name=name,
            datatype=DataType.VARCHAR,
            max_length=size,
            description=description,
            **options,
        )


def _add_tags(schema, name, *, max_capacity=64, max_length=256):
    schema.add_field(
        field_name=name,
        datatype=DataType.ARRAY,
        element_type=DataType.VARCHAR,
        max_capacity=max_capacity,
        max_length=max_length,
        description="字符串标签数组，未知时传 []",
    )


def _base_schema(dense_dim):
    if isinstance(dense_dim, bool) or not isinstance(dense_dim, int) or dense_dim <= 0:
        raise ValueError("dense_dim 必须为正整数，并与实际 Embedding 模型输出一致")
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(
        field_name="chunk_id", datatype=DataType.VARCHAR, max_length=64,
        is_primary=True, description="应用生成的稳定 UUID 字符串，任务内重试复用",
    )
    _add_strings(schema, (
        ("record_id", 64, "MongoDB 完整内容记录 ID"),
        ("document_id", 64, "原始文档 ID"),
        ("task_id", 64, "导入任务 ID，用于发布校验和失败清理"),
        ("content", 65535, "实际参与向量化的检索文本，限制为 UTF-8 字节数"),
        ("source_file_name", 1024, "原始来源文件名，含扩展名"),
        ("source_object_key", 2048, "原文件在 MinIO 中的对象键"),
        ("source_format", 16, "md/pdf/docx；doc 需额外配置旧版 Word 适配器"),
        ("embedding_model", 256, "模型名称和固定版本，同一集合保持一致"),
    ))
    schema.add_field(
        field_name="chunk_index", datatype=DataType.INT32,
        description="片段在完整记录内的顺序，从 0 开始",
    )
    _add_tags(schema, "knowledge_points")
    schema.add_field(
        field_name="source_locator", datatype=DataType.JSON,
        description="标题路径、段落、页码或行号，以及转换产物定位信息；未知传 {}",
    )
    schema.add_field(
        field_name="created_at", datatype=DataType.INT64,
        description="UTC Unix 毫秒时间戳，同一任务重试保持原创建时间",
    )
    schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=dense_dim)
    schema.add_field(field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR)
    return schema


def _add_course_context(schema, *, require_series=False):
    _add_strings(schema, (
        ("series_id", 128, "课程系列的业务编码，对应原资料系列编码"),
        ("series_name", 512, "课程系列名称"),
    ), optional=not require_series)
    _add_strings(schema, (
        ("course_id", 128, "子课程业务编码，不能用系列编码代替"),
        ("course_name", 512, "子课程名称"),
    ), optional=True)


def build_courses_schema(dense_dim: int = DEFAULT_DENSE_DIM) -> CollectionSchema:
    """课程系列/子课程检索集合。完整层级和详情保存在 MongoDB。

    entity_type=series 时 course_id/course_name 为空串；entity_type=course
    时两者必须由业务校验保证非空。课时和学时未知时传 None，不能用 0 代替。
    """
    schema = _base_schema(dense_dim)
    _add_course_context(schema, require_series=True)
    _add_strings(schema, (("entity_type", 16, "series 或 course"),))
    _add_tags(schema, "category_path", max_capacity=16)
    _add_tags(schema, "audience", max_capacity=32, max_length=128)
    _add_tags(schema, "learning_goals", max_capacity=32)
    _add_tags(schema, "grade_levels", max_capacity=32, max_length=128)
    schema.add_field(
        field_name="lesson_count", datatype=DataType.INT32, nullable=True,
        description="原文课时，未知传 None",
    )
    schema.add_field(
        field_name="study_hours", datatype=DataType.DOUBLE, nullable=True,
        description="原文学时，未知传 None",
    )
    return schema


def build_document_chunks_schema(dense_dim: int = DEFAULT_DENSE_DIM) -> CollectionSchema:
    """课程讲义和项目文档切片集合。归属未知时留空，不凭文件名强行挂课。"""
    schema = _base_schema(dense_dim)
    _add_course_context(schema)
    _add_strings(schema, (("content_type", 32, "document_chunk 或 project_material"),))
    _add_strings(schema, (
        ("project_id", 128, "项目业务标识"),
        ("project_name", 512, "项目名称"),
        ("chapter_id", 128, "章节业务标识"),
        ("chapter_name", 512, "章节名称"),
        ("title", 1024, "当前标题"),
        ("parent_title", 1024, "上级标题"),
    ), optional=True)
    return schema


def build_questions_schema(dense_dim: int = DEFAULT_DENSE_DIM) -> CollectionSchema:
    """题目检索集合。完整题干、选项、答案和解析保存在 MongoDB。

    question_type 使用 QUESTION_TYPE_LABELS 中的编码；question_type_name
    保留原始题型。无法识别但原文有题型时用 other，缺失则两字段留空。
    """
    schema = _base_schema(dense_dim)
    _add_course_context(schema)
    _add_strings(schema, (
        ("question_bank_id", 128, "原资料题库编码"),
        ("question_bank_name", 512, "题库名称"),
        ("question_code", 256, "原题编码，结合题库定位；原文缺失则留空"),
        ("question_type", 32, "规范化题型编码，参见 QUESTION_TYPE_LABELS"),
        ("question_type_name", 128, "原始题型名称，避免丢失新题型"),
    ), optional=True)
    return schema


def _build_indexes(extra_fields):
    indexes = MilvusClient.prepare_index_params()
    indexes.add_index(
        field_name="dense_vector", index_name="dense_vector_idx",
        index_type="HNSW", metric_type="COSINE",
        params={"M": 16, "efConstruction": 200},
    )
    indexes.add_index(
        field_name="sparse_vector", index_name="sparse_vector_idx",
        index_type="SPARSE_INVERTED_INDEX", metric_type="IP",
    )
    for name in ("record_id", "document_id", "task_id", "series_id", "course_id", *extra_fields):
        indexes.add_index(field_name=name, index_name=f"{name}_idx", index_type="INVERTED")
    return indexes


def build_courses_index_params():
    return _build_indexes(("entity_type",))


def build_document_chunks_index_params():
    return _build_indexes(("content_type", "project_id", "chapter_id"))


def build_questions_index_params():
    return _build_indexes(("question_bank_id", "question_code", "question_type"))
