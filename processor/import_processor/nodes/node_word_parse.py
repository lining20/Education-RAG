"""Word 分支：本地提取内容和原位置图片，保留资源至公共图片处理结束。"""
import asyncio
from contextlib import nullcontext
from common.errors import OperationError
from common.logging.logger import node_log, step_log
from processor.import_processor.state import ImportState, DataDict
from utils.node_execution import ImportDependencies
from utils.parsers.common import ParseInput
from utils.task_utils import add_running_task, add_done_task
from utils.import_cancellation import check_import_cancelled


@step_log("step_1_parse_word")
async def step_1_parse_word(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    if deps.parse_session is None or deps.document_parsers is None:
        raise OperationError("MISSING_PARSER_DEPENDENCY", "缺少解析器或解析会话")
    parser = deps.document_parsers.get("docx")
    if parser is None:
        raise OperationError("MISSING_PARSER_DEPENDENCY", "缺少 DOCX 解析器")
    async with deps.parse_semaphore if deps.parse_semaphore is not None else nullcontext():
        check_import_cancelled(deps.stop_event)
        result = await asyncio.to_thread(deps.parse_session.open, parser, ParseInput(
            state["file_path"], state["document_id"], deps.resource_context, deps.stop_event))
    return {"parsed_blocks": result.blocks}


@node_log("node_word_parse")
async def node_word_parse(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    # 1. 添加节点到运行时列表。
    started_at = await add_running_task(state, "word_parse", tasks=deps.tasks)
    # 2. 调用 DOCX 本地解析工具，保留标题、代码和原位置图片。
    update = await step_1_parse_word(state, deps=deps)
    # 3. 登记完成并返回标准内容块。
    await add_done_task(state, "word_parse", tasks=deps.tasks, started_at=started_at)
    return update
