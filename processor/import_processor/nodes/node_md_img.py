"""MD 解析与三格式公共图片处理，归档后统一为内容块。"""
import asyncio
from contextlib import nullcontext
from common.errors import OperationError
from common.logging.logger import logger, node_log, step_log
from processor.import_processor.state import ImportState, DataDict
from utils.node_execution import ImportDependencies
from utils.parsers.common import ParseInput
from utils.parsers.resources import archive_resources
from utils.parsers.image_understanding import enrich_image_descriptions
from utils.content_builders.metadata import validate_content_structure
from utils.storage_keys import document_prefix
from utils.task_utils import add_running_task, add_done_task
from utils.import_cancellation import check_import_cancelled


@step_log("step_1_prepare_images")
async def step_1_prepare_images(state: ImportState, *, deps: ImportDependencies):
    """复用三种格式的解析结果，临时图片持续保留至识别和上传结束。"""
    session = deps.parse_session
    if session is None or deps.document_parsers is None:
        raise OperationError("MISSING_PARSER_DEPENDENCY", "缺少解析会话或解析器")
    def prepare():
        if state["source_file"]["format"] == "md":
            session.open(deps.document_parsers["md"], ParseInput(
                state["file_path"], state["document_id"], deps.resource_context, deps.stop_event))
        if session.result is None:
            raise OperationError("MISSING_PARSE_RESULT", "PDF/Word 分支未产生解析结果")
        validate_content_structure(session.result.blocks, state["upload_metadata"])
        return session.result
    async with deps.parse_semaphore if deps.parse_semaphore is not None else nullcontext():
        check_import_cancelled(deps.stop_event)
        return await asyncio.to_thread(prepare)


@step_log("step_2_understand_images")
async def step_2_understand_images(state, result, *, deps):
    """图文上下文交给视觉工具，说明关联原图片块，不改原文及业务标签。"""
    if not deps.image_understanding_enabled:
        logger.info("已跳过图片视觉识别，继续归档原图：图片数={}", len(result.resources))
        return
    await enrich_image_descriptions(result, file_title=state["source_file"]["file_name"],
                                   describer=deps.image_describer, stop_event=deps.stop_event,
                                   semaphore=deps.image_semaphore, rate_limiter=deps.image_rate_limiter,
                                   concurrency=deps.image_concurrency)


@step_log("step_3_archive_images")
async def step_3_archive_images(state, result, *, deps):
    """图片与识别说明关联 MinIO 对象键，仅返回可序列化的状态增量。"""
    return await asyncio.to_thread(archive_resources,
        result, task_id=state["task_id"], document_id=state["document_id"],
        base_prefix=document_prefix(state["source_file"]["object_key"]),
        minio_client=deps.minio_client, minio_bucket_name=deps.minio_bucket_name,
        existing_warnings=state["warnings"], stop_event=deps.stop_event)


@node_log("node_md_img")
async def node_md_img(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    """运行登记 → 准备图片 → 按配置识别 → 图片归档 → 释放临时资源 → 完成登记。"""
    # 1. 登记运行，衔接原文件归档或 PDF/Word 分支。
    started_at = await add_running_task(state, "md_img", tasks=deps.tasks)
    try:
        # 2. MD 解析或读取 PDF/Word 分支结果，按引用位置收集图片。
        result = await step_1_prepare_images(state, deps=deps)
        # 3. 仅启用时调用视觉模型；关闭时保留全部原图，直接进入归档。
        await step_2_understand_images(state, result, deps=deps)
        # 4. 上传原图，并将说明和对象键一起写入内容块。
        update = await step_3_archive_images(state, result, deps=deps)
    finally:
        # 5. 识别和上传全部结束后才清理；异常也必须释放解析资源。
        if deps.parse_session is not None:
            await asyncio.to_thread(deps.parse_session.close)
    # 6. 业务成功后登记完成，交给公共切分节点。
    await add_done_task(state, "md_img", tasks=deps.tasks, started_at=started_at)
    return update
