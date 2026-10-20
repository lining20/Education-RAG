"""BGE-M3 单例与混合向量编码；模型按需加载，同一实例串行使用。"""
from threading import RLock
from common.config.model_config import embedding_config
from common.logging.logger import logger
from common.errors import OperationError

_model = None
_lock = RLock()


def get_bge_m3_ef():
    global _model
    with _lock:
        if _model is None:
            from pymilvus.model.hybrid import BGEM3EmbeddingFunction
            logger.info("加载 BGE-M3：device={}", embedding_config.bge_device)
            _model = BGEM3EmbeddingFunction(
                model_name=embedding_config.model_name_or_path,
                device=embedding_config.bge_device,
                use_fp16=embedding_config.use_fp16,
                normalize_embeddings=True,
            )
        return _model


def generate_embeddings(texts: list[str]) -> dict:
    if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("向量输入必须是非空文本列表")
    with _lock:
        model = get_bge_m3_ef()
        # 完整长代码不再硬切；先校验 token 长度，避免编码器静默截断。
        long_texts = [text for text in texts if len(text.encode("utf-8")) > 6000]
        if long_texts:
            lengths = model.model.tokenizer(long_texts, truncation=False, return_length=True,
                                            return_attention_mask=False)["length"]
            if any(length > 8192 for length in lengths):
                raise OperationError("EMBEDDING_INPUT_TOO_LONG", "完整内容超过 BGE-M3 的 8192 token 上限，请将超长代码整理为独立示例后重新导入；未截断代码")
        result = model.encode_documents(texts)
    dense = [row.tolist() for row in result["dense"]]
    sparse = result["sparse"].tocsr()
    weights = []
    for i in range(len(texts)):
        start, end = sparse.indptr[i:i + 2]
        weights.append({int(k): float(v) for k, v in zip(sparse.indices[start:end], sparse.data[start:end])})
    return {"dense": dense, "sparse": weights}
