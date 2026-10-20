"""PDF 分支：复用 MinerU 工具；图片目录保留到公共资源节点结束。"""
import asyncio
from contextlib import nullcontext
from common.errors import OperationError
from common.logging.logger import node_log, step_log
from processor.import_processor.state import ImportState, DataDict
from utils.node_execution import ImportDependencies
from utils.parsers.common import ParseInput
from utils.task_utils import add_running_task, add_done_task
from utils.import_cancellation import check_import_cancelled


@step_log("step_1_parse_pdf")
async def step_1_parse_pdf(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    """页数/大小分段及页码恢复由 PDF 工具负责，本步骤不切向量文本。"""
    if deps.parse_session is None or deps.document_parsers is None:
        raise OperationError("MISSING_PARSER_DEPENDENCY", "缺少解析器或解析会话")
    parser = deps.document_parsers.get("pdf")
    if parser is None:
        raise OperationError("MISSING_PARSER_DEPENDENCY", "缺少 PDF 解析器")
    async with deps.parse_semaphore if deps.parse_semaphore is not None else nullcontext():
        check_import_cancelled(deps.stop_event)
        result = await asyncio.to_thread(deps.parse_session.open, parser, ParseInput(
            state["file_path"], state["document_id"], deps.resource_context, deps.stop_event))
    return {"parsed_blocks": result.blocks}


@node_log("node_pdf_parse")
async def node_pdf_parse(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    # 1. 添加节点到运行时列表。
    started_at = await add_running_task(state, "pdf_parse", tasks=deps.tasks)
    # 2. 调用 PDF 解析工具，保持资源上下文有效。
    update = await step_1_parse_pdf(state, deps=deps)
    # 3. 全部成功后添加到已完成列表，返回内容块。
    await add_done_task(state, "pdf_parse", tasks=deps.tasks, started_at=started_at)
    return update
