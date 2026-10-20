"""向量化：按集合分批调用 BGE-M3，生成稠密/稀疏向量并组装待入库行。

本节点不写数据库、不发布清单；模型通过公共 embedding 工具调用，节点只做
批次、维度、数量的校验与字段组装，异常全部向上抛出，不返回部分向量。
"""

import asyncio
import math
from copy import deepcopy

from common.config.model_config import embedding_config
from common.errors import OperationError
from common.logging.logger import logger, node_log, step_log
from processor.import_processor.state import DataDict, ImportState
from utils.lm.embedding_utils import generate_embeddings
from utils.node_execution import ImportDependencies
from utils.import_cancellation import check_import_cancelled
from utils.task_utils import add_done_task, add_running_task

COLLECTIONS = ("document_index", "courses", "document_chunks", "questions")
BATCH_SIZE = 32
DENSE_DIM = 1024


def _is_timeout(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, ConnectionError)) or "timeout" in type(exc).__name__.lower()


def _validate_vectors(vectors, expected: int) -> tuple[list, list]:
    """校验一批向量：数量一致、稠密 1024 维、稀疏为 {int: float}。"""
    if not isinstance(vectors, dict):
        raise OperationError("EMBEDDING_FAILED", "向量工具返回结构无效")
    dense, sparse = vectors.get("dense"), vectors.get("sparse")
    if not isinstance(dense, list) or not isinstance(sparse, list):
        raise OperationError("EMBEDDING_FAILED", "向量工具缺少 dense/sparse 列表")
    if len(dense) != expected or len(sparse) != expected:
        raise OperationError("EMBEDDING_FAILED", "向量数量与输入文本数量不一致")
    for vector in dense:
        if (not isinstance(vector, list) or len(vector) != DENSE_DIM
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector)):
            raise OperationError("EMBEDDING_FAILED", "稠密向量维度不是 1024")
    checked = []
    for item in sparse:
        if not isinstance(item, dict):
            raise OperationError("EMBEDDING_FAILED", "稀疏向量必须是字典")
        clean = {}
        for key, value in item.items():
            if isinstance(key, bool) or not isinstance(key, int) or key < 0:
                raise OperationError("EMBEDDING_FAILED", "稀疏向量索引必须是非负整数")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise OperationError("EMBEDDING_FAILED", "稀疏向量权重必须是数值")
            clean[int(key)] = float(value)
        checked.append(clean)
    return [list(vector) for vector in dense], checked


@step_log("step_1_collect_texts")
def step_1_collect_texts(state: ImportState) -> list[DataDict]:
    """按集合收集检索文本并切成 ≤32 条的小批；没有片段时明确失败。"""
    chunks = state.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise OperationError("EMPTY_EMBEDDING_INPUT", "没有待向量化的检索片段")

    grouped: dict[str, list] = {key: [] for key in COLLECTIONS}
    index_chunks = [{"collection_key": "document_index", "fields": row} for row in state["index_drafts"]]
    for chunk in [*index_chunks, *chunks]:
        collection = chunk.get("collection_key")
        if collection not in grouped:
            raise OperationError("INVALID_CHUNK_COLLECTION", "检索片段的目标集合无效")
        text = (chunk.get("fields") or {}).get("index_content" if collection == "document_index" else "content")
        if not isinstance(text, str) or not text.strip():
            raise OperationError("EMPTY_CHUNK_CONTENT", "检索片段正文为空，不能向量化")
        grouped[collection].append(chunk)

    batches = []
    for collection, items in grouped.items():
        for start in range(0, len(items), BATCH_SIZE):
            batch = items[start:start + BATCH_SIZE]
            batches.append({"collection": collection, "rows": batch,
                            "texts": [row["fields"].get("index_content", row["fields"].get("content")) for row in batch]})
    if not any(batch["rows"] for batch in batches):
        raise OperationError("EMPTY_EMBEDDING_INPUT", "四个集合都没有待向量化的片段")
    return batches


@step_log("step_2_generate_vectors")
async def step_2_generate_vectors(state: ImportState, batches: list, *, deps: ImportDependencies) -> list[DataDict]:
    """在线程中批量调用模型；任一失败整批抛错，不返回部分向量。"""
    results = []
    for batch in batches:
        check_import_cancelled(deps.stop_event)
        try:
            vectors = await asyncio.to_thread(generate_embeddings, batch["texts"])
        except OperationError:
            raise
        except Exception as exc:
            logger.error("向量生成失败：collection={} 数量={}", batch["collection"], len(batch["texts"]))
            raise OperationError("EMBEDDING_FAILED", "向量生成失败，请通过任务标识核对日志",
                                 retryable=_is_timeout(exc)) from exc
        dense, sparse = await asyncio.to_thread(_validate_vectors, vectors, len(batch["texts"]))
        results.append({**batch, "dense": dense, "sparse": sparse})
    return results


@step_log("step_3_assemble_rows")
def step_3_assemble_rows(state: ImportState, batches: list) -> DataDict:
    """拼装四集合待 upsert 行；空稀疏向量保留 {} 并记警告，不静默丢行。"""
    embedded = {key: [] for key in COLLECTIONS}
    warnings = list(state.get("warnings") or [])
    model = embedding_config.model_name_or_path
    for batch in batches:
        for row, dense, sparse in zip(batch["rows"], batch["dense"], batch["sparse"]):
            if not sparse:
                warnings.append({"code": "SPARSE_VECTOR_EMPTY",
                                 "message": "稀疏向量为空，保留空字典并继续",
                                 "block_id": row["fields"].get("chunk_id", "")})
            embedded[batch["collection"]].append({
                **row["fields"],
                "dense_vector": dense,
                "sparse_vector": sparse,
                "embedding_model": model,
            })
    logger.info("向量组装完成：document_index={}，courses={}，document_chunks={}，questions={}，警告={}",
                len(embedded["document_index"]), len(embedded["courses"]), len(embedded["document_chunks"]),
                len(embedded["questions"]), len(warnings))
    return {"embedded_records": embedded, "warnings": warnings}


@step_log("step_4_count")
def step_4_count(state: ImportState, embedded: DataDict) -> DataDict:
    """按实际组装成功的行数统计，不用计划数代替。"""
    counts = deepcopy(state["counts"])
    counts["embedded_by_collection"] = {key: len(embedded[key]) for key in COLLECTIONS}
    return counts


@node_log("node_bge_embedding")
async def node_bge_embedding(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    """组织向量化步骤，全部成功后登记完成并返回增量。"""
    # 1. 添加 bge_embedding 到运行时列表，沿用 metadata_recognition 已完成的阶段。
    started_at = await add_running_task(state, "bge_embedding", tasks=deps.tasks)

    # 2. 按集合收集待编码检索文本，空输入明确失败。
    batches = await asyncio.to_thread(step_1_collect_texts, state)

    # 3. 分批调用 BGE-M3 生成稠密/稀疏向量，线程执行、不阻塞事件循环。
    vectors = await step_2_generate_vectors(state, batches, deps=deps)

    # 4. 组装四集合待 upsert 行，校验维度、数量与模型标识。
    assembled = await asyncio.to_thread(step_3_assemble_rows, state, vectors)
    embedded = assembled["embedded_records"]

    # 5. 汇总各集合向量化数量。
    counts = await asyncio.to_thread(step_4_count, state, embedded)

    # 6. 全部步骤成功后添加 bge_embedding 到已完成列表。
    await add_done_task(state, "bge_embedding", tasks=deps.tasks,
                        started_at=started_at, counts=counts)

    # 7. 返回 embedded_records 与 counts 增量。
    return {"embedded_records": embedded, "counts": counts, "warnings": assembled["warnings"]}

