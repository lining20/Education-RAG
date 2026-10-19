from FlagEmbedding import FlagReranker
from common.config.reranker_config import reranker_config

_reranker_model = None

def get_reranker_model():
    global _reranker_model  
    if _reranker_model is None:
        _reranker_model= FlagReranker(
            model_name_or_path=reranker_config.model_name_or_path,
            devices=reranker_config.bge_reranker_device,
            use_fp16=reranker_config.use_fp16
        )
    return _reranker_model

def compute_token_number( content: str):
    """
      计算字符串对应的token的数量
    :param content:
    :return:
    """
    reranker_model = get_reranker_model()
    tokenizer = reranker_model.tokenizer
    token_list = tokenizer.encode(content, add_special_tokens=False)
    return len(token_list)
