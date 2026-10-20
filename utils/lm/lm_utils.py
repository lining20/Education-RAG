# 环境配置与依赖导入
from langchain_openai import ChatOpenAI
from langchain_core.exceptions import LangChainException
from typing import Optional

# 项目内部依赖
from common.config.lm_config import lm_config
from common.logging.logger import logger

# 全局缓存：键为(模型名, JSON输出模式, SDK重试次数)，值为ChatOpenAI实例
# 作用：避免重复初始化客户端，提升性能，统一实例管理
_llm_client_cache = {}


def get_llm_client(model: Optional[str] = None, json_mode: bool = False, *, max_retries: int = 2) -> ChatOpenAI:
    """
    获取带全局缓存的LangChain ChatOpenAI客户端实例
    适配OpenAI/千问/即梦AI等**OpenAI兼容API**，支持自定义模型和JSON标准化输出
    核心特性：缓存机制+配置统一加载+异常精准捕获+国产模型参数适配

    :param model: 模型名称，优先级：传入参数 > 配置文件lm_config.llm_model > 内置默认qwen3-32b
    :param json_mode: 是否开启JSON输出模式，开启后返回标准json_object格式（适配结构化数据解析）
    :param max_retries: SDK重试次数；视觉限流请求设为0，避免自动重试绕过请求配额。
    :return: 初始化完成的ChatOpenAI实例（优先从全局缓存获取，未命中则新建并缓存）
    :raise ValueError: 缺失API密钥/基础地址等核心配置
    :raise Exception: 模型初始化失败（LangChain封装层异常）
    """
    # 1. 确定目标模型（优先级递减，保证模型名非空）
    target_model = model or lm_config.llm_model or "qwen3-32b"
    # 缓存键：模型名+JSON模式，唯一标识不同配置的客户端
    cache_key = (target_model, json_mode, max_retries)

    # 2. 缓存命中：直接返回已初始化的实例，避免重复创建
    if cache_key in _llm_client_cache:
        logger.debug(f"[LLM客户端] 缓存命中，直接返回实例：模型={target_model}，JSON模式={json_mode}")
        return _llm_client_cache[cache_key]

    # 3. 核心配置校验：拦截缺失的API关键配置，提前抛出明确异常
    lm_config.validate()
    logger.info(f"[LLM客户端] 开始初始化新实例：模型={target_model}，JSON模式={json_mode}")

    # 4. 私有参数按模型选择，不能把千问参数发给 Kimi 等其他模型。
    model_name = target_model.rsplit("/", 1)[-1].lower()
    extra_body = {"enable_thinking": False} if model_name.startswith(("qwen", "qwq")) else None
    temperature = lm_config.llm_temperature
    client_options = {}
    if model_name.startswith("kimi-k3"):
        # K3 固定采样参数且始终思考；省略 temperature，低思考强度适合检索链。
        temperature = None
        client_options["reasoning_effort"] = "low"
        logger.debug("[LLM客户端] Kimi K3 使用默认采样参数，reasoning_effort=low")
    # model_kwargs：OpenAI通用参数，所有兼容API均支持
    model_kwargs = {}
    if json_mode:
        # 开启JSON标准输出模式，强制模型返回可解析的json_object
        model_kwargs["response_format"] = {"type": "json_object"}
        logger.debug(f"[LLM客户端] 已开启JSON输出模式，模型将返回标准JSON结构")

    # 5. 客户端初始化：捕获LangChain封装层异常，抛出更友好的提示
    try:
        llm_client = ChatOpenAI(
            model=target_model,  # 目标模型名
            temperature=temperature,
            api_key=lm_config.api_key,  # API密钥
            base_url=lm_config.base_url,  # API基础地址（适配国产模型代理地址）
            extra_body=extra_body,  # 国产模型私有参数透传
            model_kwargs=model_kwargs,  # OpenAI通用参数
            max_retries=max_retries,
            **client_options,
        )
    except LangChainException as e:
        raise Exception(f"[LLM客户端] 模型【{target_model}】初始化失败（LangChain层）：{str(e)}") from e

    # 6. 新实例存入全局缓存，供后续调用复用
    _llm_client_cache[cache_key] = llm_client
    logger.info(f"[LLM客户端] 实例初始化成功并缓存：模型={target_model}，JSON模式={json_mode}")

    return llm_client


# 测试示例：验证客户端创建、缓存机制及日志输出
