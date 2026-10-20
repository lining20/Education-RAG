"""Reranker 按需加载；初始化和推理共用锁。"""
from threading import RLock
from common.config.model_config import reranker_config

_reranker_model = None
_lock = RLock()

def get_reranker_model():
    global _reranker_model  
    with _lock:
        if _reranker_model is None:
            from FlagEmbedding import FlagReranker
            _reranker_model = FlagReranker(
                model_name_or_path=reranker_config.model_name_or_path,
                devices=reranker_config.bge_reranker_device,
                use_fp16=reranker_config.use_fp16)
    return _reranker_model

def compute_token_number( content: str):
    """
      计算字符串对应的token的数量
    :param content:
    :return:
    """
    with _lock:
        return len(get_reranker_model().tokenizer.encode(content, add_special_tokens=False))


def rerank_documents(query: str, texts: list[str]) -> list[float]:
    if not texts:
        return []
    with _lock:
        scores = get_reranker_model().compute_score([[query, text] for text in texts], normalize=True)
    if hasattr(scores, "tolist"):
        scores = scores.tolist()
    return [float(scores)] if isinstance(scores, (int, float)) else [float(score) for score in scores]
