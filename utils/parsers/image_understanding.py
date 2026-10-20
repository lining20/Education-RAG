"""公共图片理解：在解析上下文存活期间调用，保留原文并关联生成说明。"""
import base64
import asyncio
import inspect
from hashlib import file_digest
from pathlib import Path

from langchain_core.messages import HumanMessage

from common.errors import OperationError
from common.logging.logger import logger
from utils.load_prompt import load_prompt
from utils.parsers.common import ParseResult, ResourceAsset
from utils.parsers.vision_image import prepare_vision_image
from utils.import_cancellation import check_import_cancelled
from utils.rate_limit_utils import ApiRateLimiter, apply_api_rate_limit


async def describe_image(asset: ResourceAsset, context: str, *, file_title: str,
                         rate_limiter: ApiRateLimiter, stop_event=None) -> dict:
    """复用现有模型客户端和配置；无图不调用，视觉模型缺失时不回退到文本模型。"""
    from common.config.lm_config import lm_config
    from utils.lm.lm_utils import get_llm_client

    if not lm_config.vl_model:
        raise OperationError("MISSING_VISION_MODEL", "文档含图片，请配置 VL_MODEL、OPENAI_BASE_URL 和 OPENAI_API_KEY")
    # 格式转换仍是同步操作，取消时先等该线程退出，避免提前删除正在读取的图片。
    conversion = asyncio.create_task(asyncio.to_thread(prepare_vision_image, asset))
    try:
        image_data, media_type = await asyncio.shield(conversion)
    except asyncio.CancelledError:
        await conversion
        raise
    prompt = load_prompt("image_summary", file_title=file_title, context=context)
    encoded = base64.b64encode(image_data).decode("ascii")
    client = get_llm_client(lm_config.vl_model, max_retries=0)
    # 与参考项目相同：真正请求模型前限流；异步等待保持可取消。
    await apply_api_rate_limit(rate_limiter, stop_event=stop_event)
    try:
        response = await asyncio.wait_for(client.ainvoke([HumanMessage(content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{encoded}"}},
        ])]), timeout=90)
    except Exception as exc:
        raise OperationError("IMAGE_UNDERSTANDING_FAILED", "图片视觉识别失败，请检查模型服务后重试", retryable=True) from exc
    content = response.content
    if isinstance(content, list):
        content = "\n".join(part.get("text", "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text")
    return {"text": content, "model": lm_config.vl_model}


async def enrich_image_descriptions(result: ParseResult, *, file_title: str, describer=None,
                                    stop_event=None, semaphore=None, rate_limiter=None, concurrency=8) -> None:
    """按 Service 配置创建 worker，共享视觉配额；结果按原引用顺序写回。"""
    limiter = semaphore if semaphore is not None else asyncio.Semaphore(concurrency)
    request_limiter = rate_limiter if rate_limiter is not None else ApiRateLimiter()
    jobs = []
    for asset in result.resources:
        occurrences = [(index, block) for index, block in enumerate(result.blocks)
                       if asset.resource_id in block["payload"].get("resource_ids", [])]
        if occurrences:
            jobs.append((asset, occurrences))
    if not jobs:
        return
    logger.info("图片识别开始：图片数={}，单文档最多{}个并发 worker，共享服务配额", len(jobs), concurrency)
    completed = [None] * len(jobs)
    pending = iter(enumerate(jobs))
    completed_count = 0

    async def recognize(asset, occurrences):
        contexts = []
        for index, block in occurrences:
            before = result.blocks[index - 1]["text"][-300:] if index else ""
            after = result.blocks[index + 1]["text"][:300] if index + 1 < len(result.blocks) else ""
            contexts.append("标题：" + " / ".join(block["heading_path"]) +
                            "\n上文：" + before + "\n原位置文字：" + block["text"][:300] + "\n下文：" + after)
        context = "\n\n".join(dict.fromkeys(contexts))[:6000]

        async def invoke():
            if describer is None:
                return await describe_image(asset, context, file_title=file_title,
                                            rate_limiter=request_limiter, stop_event=stop_event)
            await apply_api_rate_limit(request_limiter, stop_event=stop_event)
            if inspect.iscoroutinefunction(describer):
                return await describer(asset, context)
            # 同步注入工具取消时等其线程结束，避免图片目录被提前删除。
            work = asyncio.create_task(asyncio.to_thread(describer, asset, context))
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                await work
                raise

        async with limiter:
            check_import_cancelled(stop_event)
            logger.info("开始图片视觉识别：resource_id={}，引用数={}", asset.resource_id, len(occurrences))
            request = asyncio.create_task(invoke())
            try:
                while not request.done():
                    check_import_cancelled(stop_event)
                    await asyncio.wait({request}, timeout=0.1)
                check_import_cancelled(stop_event)
                output = await request
            except OperationError as exc:
                raise OperationError(exc.code, f"{exc.message}（资源 {asset.resource_id}）", retryable=exc.retryable) from exc
            finally:
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)
        text, model = output.get("text"), output.get("model")
        if not isinstance(text, str) or not text.strip() or not isinstance(model, str) or not model.strip():
            raise OperationError("EMPTY_IMAGE_DESCRIPTION", f"图片识别未返回有效说明或模型标识（资源 {asset.resource_id}）")
        decorative = text.strip() == "[装饰图片]"
        with Path(asset.file_path).open("rb") as stream:
            fingerprint = file_digest(stream, "sha256").hexdigest()
        return {"resource_id": asset.resource_id, "text": "" if decorative else text.strip(),
                "model": model, "origin": "vision_model", "fingerprint": fingerprint,
                "status": "decorative" if decorative else "recognized"}

    async def worker():
        nonlocal completed_count
        for index, (asset, occurrences) in pending:
            check_import_cancelled(stop_event)
            for attempt in range(3):
                try:
                    completed[index] = await recognize(asset, occurrences)
                    break
                except Exception as exc:
                    check_import_cancelled(stop_event)
                    if isinstance(exc, OperationError) and exc.code == "IMPORT_CANCELLED":
                        raise
                    retryable = not isinstance(exc, OperationError) or exc.retryable
                    if retryable and attempt < 2:
                        logger.warning("图片识别失败，准备重试：resource_id={}，重试={}/2", asset.resource_id, attempt + 1)
                        # 每次重试仍经过共享限流，退避期间可响应服务关闭。
                        for _ in range(10 * (attempt + 1)):
                            check_import_cancelled(stop_event)
                            await asyncio.sleep(.1)
                        continue
                    code = exc.code if isinstance(exc, OperationError) else "IMAGE_UNDERSTANDING_FAILED"
                    completed[index] = {"resource_id": asset.resource_id, "text": "", "model": "",
                                        "origin": "vision_model", "status": "unrecognized", "error_code": code}
                    logger.warning("图片未识别，保留原图继续导入：resource_id={}，code={}", asset.resource_id, code)
                    break
            completed_count += 1
            logger.info("图片识别进度：{}/{}，resource_id={}", completed_count, len(jobs), asset.resource_id)

    workers = [asyncio.create_task(worker()) for _ in range(min(concurrency, len(jobs)))]
    try:
        await asyncio.gather(*workers)
    finally:
        # 关闭或文件读取等不可恢复故障时，等待请求退出再释放临时目录。
        for work in workers:
            if not work.done():
                work.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    for (_, occurrences), description in zip(jobs, completed):
        if description["status"] == "unrecognized":
            result.warnings.append({"code": "IMAGE_NOT_RECOGNIZED",
                "message": "图片未识别，已保留原图；文字内容继续导入",
                "resource_id": description["resource_id"], "cause_code": description["error_code"]})
        for _, block in occurrences:
            block["payload"].setdefault("image_descriptions", []).append(dict(description))


def image_supplement(blocks) -> dict:
    """课程/题目只追加补充检索文本，不将模型说明解析为课程属性或题目答案。"""
    resources, descriptions = {}, {}
    for block in blocks:
        for resource in block["payload"].get("resources", []):
            resources[resource["resource_id"]] = resource
        for item in block["payload"].get("image_descriptions", []):
            descriptions[item["resource_id"]] = {**item, "source_locator": dict(block["source_locator"])}
    return {"resources": list(resources.values()), "image_descriptions": list(descriptions.values())}


def image_supplement_text(supplement: dict) -> str:
    return "\n\n".join("[图片内容说明·视觉模型生成]\n" + item["text"]
                        for item in supplement["image_descriptions"] if item["text"])
