"""双层混合检索、清单核验与完整业务记录恢复；不扫描未发布内容。"""
from copy import deepcopy
import json
from urllib.parse import quote
from itertools import zip_longest
from common.errors import OperationError
from common.logging.logger import logger
from utils.clients.milvus_collections import collection_names
from utils.clients.milvus_utils import create_hybrid_search_requests, escape_milvus_string, hybrid_search


def literal(value):
    return '"' + escape_milvus_string(value) + '"'


class QueryRetrieval:
    def __init__(self, milvus, minio, bucket, embedding_model):
        self.milvus, self.minio, self.bucket = milvus, minio, bucket
        self.embedding_model = embedding_model

    def read_json(self, key):
        response = self.minio.get_object(self.bucket, key)
        try:
            data = response.read(20 * 1024 * 1024 + 1)
            if len(data) > 20 * 1024 * 1024:
                raise OperationError("SOURCE_PAYLOAD_TOO_LARGE", "完整来源内容超过读取上限")
            return json.loads(data)
        finally:
            response.close()
            if hasattr(response, "release_conn"):
                response.release_conn()

    def published(self, row, collection, cache=None):
        cache = cache if cache is not None else {}
        key = row.get("import_manifest_key")
        if not key or row.get("embedding_model") != self.embedding_model:
            return False
        if key not in cache:
            try:
                cache[key] = self.read_json(key)
            except Exception as exc:
                if getattr(exc, "code", "") in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                    cache[key] = None
                else:
                    raise OperationError("MANIFEST_UNAVAILABLE", "无法核验知识库发布清单，请检查存储服务", retryable=True) from exc
        manifest = cache[key]
        primary = "index_id" if collection == collection_names()["document_index"] else "chunk_id"
        return bool(manifest and manifest.get("status") == "succeeded"
            and manifest.get("task_id") == row.get("task_id")
            and manifest.get("document_id") == row.get("document_id")
            and manifest.get("embedding_model") == self.embedding_model
            and row.get(primary) in manifest.get("collections", {}).get(collection, {}).get("ids", []))

    def _search(self, collection, vectors, expression, fields, limit=30):
        if not self.milvus.has_collection(collection_name=collection):
            return []
        requests = create_hybrid_search_requests(vectors["dense"][0], vectors["sparse"][0], expr=expression, limit=limit)
        # 空稀疏向量不交给 Milvus，稠密检索仍可工作。
        if not vectors["sparse"][0]:
            requests = requests[:1]
        result = hybrid_search(self.milvus, collection, requests, limit=limit, output_fields=fields)
        if result is None:
            raise OperationError("RETRIEVAL_UNAVAILABLE", "向量检索失败，请检查集合和模型配置", retryable=True)
        return [dict(hit.get("entity", {})) for hit in (result[0] if result else [])]

    def subjects(self, vectors, subject):
        if not subject:
            return []
        name = collection_names()["document_index"]
        rows = self._search(name, vectors, "embedding_model == " + literal(self.embedding_model),
            ["index_id", "document_id", "task_id", "subject_name", "embedding_model", "import_manifest_key"], limit=12)
        cache = {}
        return list(dict.fromkeys(row["document_id"] for row in rows
            if subject.casefold() in row.get("subject_name", "").casefold() and self.published(row, name, cache)))

    def retrieve(self, vectors, scope, filters, preferred_document_ids):
        """只按用户条件限制范围；自动主体命中文档仅作为候选同分排序参考。"""
        preferred = set(preferred_document_ids or [])
        # 题目专属条件仅在明确选择“题目”范围时生效，残留选择不能排除其他资料。
        if scope != "question":
            filters = {key: value for key, value in filters.items()
                       if key not in {"question_bank_name", "question_code", "question_type"}}
        names = collection_names()
        targets = {"course_intro": ["courses"], "document_chunk": ["document_chunks"],
                   "project_material": ["document_chunks"], "question": ["questions"],
                   "all": ["courses", "document_chunks", "questions"]}[scope]
        paths, cache = [], {}
        common = {"course_name", "series_name", "source_file_name"}
        for target in targets:
            allowed = common | ({"project_name", "chapter_name"} if target == "document_chunks" else
                                {"question_bank_name", "question_code", "question_type"} if target == "questions" else set())
            if any(value and field not in allowed for field, value in filters.items()):
                continue
            conditions = ["embedding_model == " + literal(self.embedding_model)]
            conditions.extend(f"{field} == {literal(value)}" for field, value in filters.items() if value)
            if scope in {"document_chunk", "project_material"}:
                conditions.append("content_type == " + literal(scope))
            collection = names[target]
            logger.info("本地检索范围：集合={}，用户筛选字段={}，主体排序参考文档数={}，无自动主体硬过滤",
                        collection, [key for key, value in filters.items() if value], len(preferred))
            found = []
            fields = ["chunk_id", "record_id", "document_id", "task_id", "content", "record_payload",
                "payload_object_key", "import_manifest_key", "embedding_model", "source_file_name",
                "source_object_key", "source_format", "source_locator", "course_name", "series_name"]
            fields.extend(["title", "project_name", "chapter_name"] if target == "document_chunks" else
                          ["question_bank_name", "question_code", "question_type"] if target == "questions" else ["entity_type"])
            for row in self._search(collection, vectors, " and ".join(conditions), fields):
                if self.published(row, collection, cache):
                    found.append({"key": collection + ":" + row["chunk_id"], "kind": "knowledge",
                                   "collection": collection, "content": row["content"], "row": row,
                                   "subject_match": row.get("document_id") in preferred})
            paths.append(found)
        # 跨集合轮流取同名次结果，避免 all 范围被排在首位的课程集合挤占。
        return [item for group in zip_longest(*paths) for item in group if item is not None]

    def restore_reference(self, reference):
        previous = reference["row"]
        rows = self.milvus.get(collection_name=reference["collection"], ids=[previous["chunk_id"]], output_fields=["*"])
        if not rows or rows[0].get("task_id") != previous["task_id"] or not self.published(rows[0], reference["collection"]):
            raise OperationError("REFERENCE_EXPIRED", "来源已更新或撤销，请重新检索获取最新资料")
        return self.hydrate({**reference, "row": rows[0], "content": rows[0]["content"]})

    def hydrate(self, candidate):
        item = deepcopy(candidate)
        if item["kind"] == "web":
            return item
        row = item["row"]
        record = item.get("record")
        if record is None:
            record = self.read_json(row["payload_object_key"]) if row.get("payload_object_key") else row.get("record_payload")
        if not isinstance(record, dict) or record.get("record_id") != row["record_id"]:
            raise OperationError("INVALID_SOURCE_RECORD", "完整业务记录与检索结果不一致")
        item["record"] = record
        for key in ("record_payload", "dense_vector", "sparse_vector"):
            row.pop(key, None)
        return item

    def references(self, candidates, session_id, request_id):
        refs, cache = [], {}
        for candidate in candidates:
            if candidate["kind"] == "knowledge" and not self.published(candidate["row"], candidate["collection"], cache):
                continue  # 文件重导入后旧任务清单失效，不能继续引用。
            item = self.hydrate(candidate)
            item["id"] = str(len(refs) + 1)
            if item["kind"] == "knowledge":
                row = item["row"]
                item["title"] = row.get("title") or row.get("course_name") or row["source_file_name"]
                base = f"/api/query/references/{quote(session_id, safe='')}/{quote(request_id, safe='')}/{item['id']}"
                item["download_url"] = base + "/file"
                resources = item["record"].get("payload", {}).get("resources", [])
                item["images"] = [{"resource_id": resource["resource_id"], "url": base + "/image/" + quote(resource["resource_id"], safe=''),
                                   "description": resource.get("description", "") or resource.get("alt", ""),
                                   "original_reference": resource.get("original_reference", ""),
                                   "heading_path": resource.get("heading_path", []),
                                   "source_locator": resource.get("source_locator", {})} for resource in resources
                                  if resource.get("media_type") in {"image/png", "image/jpeg", "image/gif", "image/webp"}]
            refs.append(item)
        return refs


def reciprocal_rank_fusion(paths):
    merged = {}
    for path in paths:
        seen = set()
        for rank, item in enumerate(path, 1):
            if item["key"] in seen:
                continue
            seen.add(item["key"])
            entry = merged.setdefault(item["key"], {**item, "rrf_score": 0})
            entry["rrf_score"] += 1 / (60 + rank)
    return sorted(merged.values(), key=lambda item: item["rrf_score"], reverse=True)[:40]
