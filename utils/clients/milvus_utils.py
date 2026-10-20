from pymilvus import MilvusClient, AnnSearchRequest, RRFRanker, WeightedRanker

from common.config.milvus_config import milvus_config
from common.logging.logger import logger            # 项目统一日志工具
# 全局Milvus客户端实例，实现单例复用
_milvus_client = None


def get_milvus_client():
    """
    Milvus客户端单例获取方法
    实现客户端连接复用，避免重复创建连接消耗资源
    :return: MilvusClient实例，连接失败返回None
    """
    try:
        global _milvus_client
        # 单例判断：未初始化则创建新连接
        if _milvus_client is None:
            milvus_config.validate()
            milvus_uri = milvus_config.milvus_url
            # 初始化Milvus客户端
            _milvus_client = MilvusClient(uri=milvus_uri, timeout=milvus_config.timeout)
            logger.info("Milvus客户端连接成功")
        return _milvus_client
    except Exception as e:
        logger.error(f"Milvus客户端连接异常：{str(e)}", exc_info=True)
        return None


def escape_milvus_string(value: str) -> str:
    """
    Milvus过滤表达式专用字符串安全转义函数
    核心作用：
        避免因原始字符串含特殊字符，导致Milvus解析filter_expr时报错，保证CRUD操作正常执行
    转义规则：
        1. 反斜杠（\）→ 双反斜杠（\\）：Milvus表达式转义规则
        2. 双引号（"）→ 转义双引号（\"）：避免截断字符串表达式
        3. 换行/回车/制表符 → 空格：防止表达式换行导致解析失败
    参数：
        value: 需要转义的原始字符串（如商品名称、文件标题）
    返回：
        str: 转义后的安全字符串，可直接用于Milvus的filter_expr
    """
    if value is None:
        return ""
    # 确保输入为字符串类型，避免非字符串值报错
    s = str(value)
    # 按Milvus规则转义特殊字符
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    # 替换换行/回车/制表符为空格，保证表达式单行有效
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return s


def create_hybrid_search_requests(dense_vector, sparse_vector, dense_params=None, sparse_params=None, expr=None,
                                  limit=5):
    """
    构建Milvus混合搜索请求对象
    分别创建稠密/稀疏向量的搜索请求，用于后续混合搜索融合
    :param dense_vector: 文本生成的稠密向量
    :param sparse_vector: 文本生成的稀疏向量
    :param dense_params: 稠密向量搜索参数，默认使用余弦相似度
    :param sparse_params: 稀疏向量搜索参数，默认使用内积相似度
    :param expr: 搜索过滤表达式，用于精准筛选数据
    :param limit: 单向量搜索返回结果数量，默认5
    :return: 搜索请求列表，包含[dense_req, sparse_req]
    """
    # 稠密向量默认搜索参数：余弦相似度（COSINE），适配BGE-M3稠密向量并与建库参数保持一致
    if dense_params is None:
        dense_params = {"metric_type": "COSINE", "params": {"ef": max(64, limit)}}
    # 稀疏向量默认搜索参数：内积（IP），适配BGE-M3稀疏向量
    if sparse_params is None:
        sparse_params = {"metric_type": "IP"}

    # 构建稠密向量搜索请求，关联Milvus的dense_vector字段 近似最近邻（ANN）检索请求的核心类
    dense_req = AnnSearchRequest(
        data=[dense_vector],
        anns_field="dense_vector",
        param=dense_params,
        expr=expr,
        limit=limit
    )

    # 构建稀疏向量搜索请求，关联Milvus的sparse_vector字段
    sparse_req = AnnSearchRequest(
        data=[sparse_vector],
        anns_field="sparse_vector",
        param=sparse_params,
        expr=expr,
        limit=limit
    )

    return [dense_req, sparse_req]


def hybrid_search(client, collection_name, reqs, ranker_weights=None, norm_score=False, limit=5,
                  output_fields=None, search_params=None):
    """
    执行Milvus稠密+稀疏向量混合搜索
    默认使用RRF融合；显式传入ranker_weights时使用加权融合。
    本函数一次只搜索一个集合，跨集合结果由查询流程另行融合和重排。
    :param client: MilvusClient实例
    :param collection_name: 集合名称
    :param reqs: 搜索请求列表，固定为[dense_req, sparse_req]
    :param ranker_weights: 可选的稠密/稀疏融合权重，None表示使用RRF
    :param norm_score: 是否归一化评分后再融合，避免评分量级差异导致权重失效
    :param limit: 混合搜索最终返回结果数量，默认5
    :param output_fields: 默认返回三个集合共有的文本与来源字段
    :param search_params: 搜索参数，如ef/topk等，默认None
    :return: 混合搜索结果列表，搜索失败返回None
    """
    try:
        rerank = (
            RRFRanker()
            if ranker_weights is None
            else WeightedRanker(ranker_weights[0], ranker_weights[1], norm_score=norm_score)
        )

        # 使用共有字段，课程、文档和题目三个集合均可调用。
        if output_fields is None:
            output_fields = [
                "chunk_id", "record_id", "task_id", "content",
                "source_file_name", "source_object_key", "source_locator",
            "record_payload", "payload_object_key", "import_manifest_key",
            ]

        # 执行混合搜索：融合稠密+稀疏向量结果，按权重重新排序
        res = client.hybrid_search(
            collection_name=collection_name,
            reqs=reqs,
            ranker=rerank,
            limit=limit,
            output_fields=output_fields,
            timeout=milvus_config.timeout,
            consistency_level="Strong",
            search_params=search_params
        )

        logger.info(f"Milvus混合搜索完成，集合[{collection_name}]共检索到{len(res[0])}条结果")
        return res
    except Exception as e:
        logger.error(f"Milvus混合搜索执行失败，集合[{collection_name}]：{str(e)}", exc_info=True)
        return None
