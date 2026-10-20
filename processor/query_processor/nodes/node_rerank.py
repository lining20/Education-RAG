"""rerank：登记运行 → 调用业务步骤 → 登记完成 → 返回状态增量。"""
from common.logging.logger import node_log
from utils.query_execution import add_running_task, add_done_task
from utils.query_steps import rerank as business_step


@node_log("node_rerank")
async def node_rerank(state, *, deps):
    # 1. 登记当前节点，前端可见运行时列表。
    started = await add_running_task(state, "rerank", deps=deps)
    # 2. 复用查询业务工具，节点内不创建客户端或重复实现算法。
    update = await business_step(state, deps)
    # 3. 业务成功后登记完成，失败交给绑定器和错误节点。
    await add_done_task(state, "rerank", deps=deps, started_at=started)
    # 4. 只返回增量；并发召回分别写入各自结果字段。
    return update

