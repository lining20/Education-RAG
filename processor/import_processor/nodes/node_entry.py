"""导入入口：检查文件与上传参数，初始化文件统计并返回增量状态。"""

import asyncio
from copy import deepcopy

from common.errors import OperationError
from common.logging.logger import node_log, step_log
from processor.import_processor.state import DataDict, ImportState
from utils.node_execution import ImportDependencies
from utils.path_util import validate_import_file
from utils.task_utils import add_done_task, add_running_task


@step_log("step_1_validate_file")
async def step_1_validate_file(state: ImportState, *, deps: ImportDependencies) -> str:
    """检查输入文件，返回校验后的绝对路径。

    输入为 State 的 file_path 和依赖中的大小限制；路径、MD/PDF/DOCX
    格式、大小及可读性由公共工具判断，失败直接抛错，不检查正文内容。
    """
    # 文件检查包含同步 I/O，在线程中执行，避免阻塞事件循环。
    file_path, _, _ = await asyncio.to_thread(
        validate_import_file,
        state.get("file_path", ""),
        max_file_bytes=deps.max_file_bytes,
    )
    return file_path


@step_log("step_2_prepare_state")
def step_2_prepare_state(state: ImportState, file_path: str) -> DataDict:
    """从输入 State 和已校验路径生成本节点的增量，不修改输入。

    upload_metadata 仅要求为字典，内容深拷贝透传；复制现有 counts，
    只将 files 设为 1，保留其他统计，不重复创建整份初始状态。
    """
    upload_metadata = state.get("upload_metadata")
    if not isinstance(upload_metadata, dict):
        raise OperationError("INVALID_UPLOAD_METADATA", "upload_metadata 必须为字典")

    # 深拷贝嵌套容器，保证后续使用增量时不会连带修改输入 State。
    counts = deepcopy(state["counts"])
    counts["files"] = 1
    return {
        "file_path": file_path,
        "upload_metadata": deepcopy(upload_metadata),
        "counts": counts,
        "status": "running",
        "current_node": "entry",
        "task_claimed": True,
    }


@node_log("node_entry")
async def node_entry(state: ImportState, *, deps: ImportDependencies) -> DataDict:
    """组织入口步骤；异常交由 bind_node 转为失败状态并登记失败进度。"""
    # 1. 添加节点到运行时列表，同时领取 pending 任务。
    started_at = await add_running_task(state, "entry", tasks=deps.tasks)

    # 2. 检查文件路径、格式、大小和可读性。
    file_path = await step_1_validate_file(state, deps=deps)

    # 3. 整理上传参数与文件统计，生成增量状态。
    update = step_2_prepare_state(state, file_path)

    # 4. 全部业务步骤成功后，添加节点到已完成列表。
    await add_done_task(
        state, "entry", tasks=deps.tasks,
        started_at=started_at, counts=update["counts"],
    )

    # 5. 返回本节点产生的增量状态。
    return update


if __name__ == "__main__":
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from common.logging.logger import init_logger, logger
    from processor.import_processor.state import create_initial_state
    from utils.node_execution import bind_node
    from utils.task_utils import ImportTaskStore

    async def self_check():
        """使用临时文件和真实内存任务，检查五个入口案例及进度列表。"""
        logger.info("node_entry 自测开始，共 5 个案例")
        tasks = ImportTaskStore()
        deps = ImportDependencies(tasks=tasks)
        entry = bind_node("entry", node_entry, deps)

        with TemporaryDirectory(prefix="node_entry_") as directory:
            root = Path(directory)
            # 使用对应扩展名的非空文件，只检查入口文件可用性，不验证正文解析。
            for suffix in ("txt", "md", "pdf", "docx"):
                (root / f"sample.{suffix}").write_bytes(b"entry file check\n")

            cases = (
                ("不支持 TXT", "sample.txt", "UNSUPPORTED_FILE_FORMAT"),
                ("正常 MD", "sample.md", None),
                ("正常 PDF（仅入口文件检查）", "sample.pdf", None),
                ("正常 DOCX（仅入口文件检查）", "sample.docx", None),
                ("缺少路径", None, "INVALID_FILE_PATH"),
            )
            for index, (description, file_name, expected_error) in enumerate(cases, 1):
                logger.info("案例 {}/5：{}", index, description)
                task_id, document_id = f"entry-test-{index}", f"entry-doc-{index}"
                tasks.create_pending(task_id, document_id)
                state = create_initial_state(
                    task_id=task_id, document_id=document_id,
                    file_path=str(root / file_name) if file_name else "",
                    upload_metadata={"course_name": "Python"},
                )
                if file_name is None:
                    state.pop("file_path")
                update = await entry(state)
                progress = tasks.get(task_id)

                assert progress["running_nodes"] == []
                if expected_error is not None:
                    assert update["status"] == "failed" and update["error"]["code"] == expected_error
                    assert progress["failed_nodes"] == ["entry"] and progress["completed_nodes"] == []
                else:
                    assert update["status"] == "running" and update["counts"]["files"] == 1
                    assert progress["completed_nodes"] == ["entry"] and progress["failed_nodes"] == []
                logger.success("案例通过：{}", description)

        logger.success("node_entry 自测结束：5/5 通过；PDF/DOCX 未验证正文解析")

    init_logger()
    asyncio.run(self_check())
