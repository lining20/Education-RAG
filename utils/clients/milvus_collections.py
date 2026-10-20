"""确保四个教育集合存在：缺少则按本地 Schema 创建，已存在只校验字段。

只创建，不覆盖、不删除、不改已有集合。字段与本地 Schema 不一致时明确报错，
避免把数据写进结构不同的集合。供启动装配与 init_collections 脚本共用。
"""

from common.config.milvus_config import milvus_config
from common.errors import OperationError
from common.logging.logger import logger
from common.schemas.milvus_schema import (
    build_document_index_schema,
    build_document_index_index_params,
    build_courses_index_params,
    build_courses_schema,
    build_document_chunks_index_params,
    build_document_chunks_schema,
    build_questions_index_params,
    build_questions_schema,
)

SPECS = (
    (milvus_config.document_index_collection, build_document_index_schema, build_document_index_index_params),
    (milvus_config.courses_collection, build_courses_schema, build_courses_index_params),
    (milvus_config.document_chunks_collection, build_document_chunks_schema,
     build_document_chunks_index_params),
    (milvus_config.questions_collection, build_questions_schema, build_questions_index_params),
)


def ensure_collections(client) -> list[str]:
    """返回本次新建的集合名；已存在且字段一致时跳过。"""
    created: list[str] = []
    for name, schema_builder, index_builder in SPECS:
        schema = schema_builder()
        expected = [field.name for field in schema.fields]
        if client.has_collection(collection_name=name):
            description = client.describe_collection(collection_name=name)
            existing = [field["name"] for field in description.get("fields", [])]
            if set(existing) != set(expected):
                raise OperationError(
                    "MILVUS_SCHEMA_MISMATCH",
                    f"集合 {name} 已存在但字段与本地 Schema 不一致，请先核对迁移，不会自动覆盖",
                )
            fields = {field["name"]: field for field in description["fields"]}
            for field in schema.fields:
                actual = fields[field.name]
                if actual.get("type") != int(field.dtype) or bool(actual.get("is_primary", False)) != field.is_primary:
                    raise OperationError("MILVUS_SCHEMA_MISMATCH", f"集合 {name} 的字段 {field.name} 类型或主键不匹配")
                for param in ("dim", "max_length"):
                    if param in field.params and str(actual.get("params", {}).get(param)) != str(field.params[param]):
                        raise OperationError("MILVUS_SCHEMA_MISMATCH", f"集合 {name} 的字段 {field.name} 容量不匹配")
            if description.get("auto_id", False):
                raise OperationError("MILVUS_SCHEMA_MISMATCH", f"集合 {name} 不允许开启 AutoID")
            continue
        client.create_collection(
            collection_name=name, schema=schema_builder(), index_params=index_builder(),
        )
        created.append(name)
        logger.success("集合 {} 不存在，已按本地 Schema 创建", name)
    return created


def collection_names() -> dict[str, str]:
    """四集合逻辑键与配置名的唯一映射。"""
    return {"document_index": milvus_config.document_index_collection,
            "courses": milvus_config.courses_collection,
            "document_chunks": milvus_config.document_chunks_collection,
            "questions": milvus_config.questions_collection}

