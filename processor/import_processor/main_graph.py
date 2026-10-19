"""导入图工厂：显式注册九个节点及成功/失败路由，不提供占位业务节点。

服务初始化时注入真实节点、共享依赖和统一执行绑定器，调用工厂编译后复用；
每个任务独立创建 ImportState。导入本模块不构图、不初始化外部服务。
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from processor.import_processor.state import ImportNodeName, ImportState

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


# 业务节点约定：async def node_xxx(state, *, deps)，只返回增量状态。
ImportNode = Callable[..., Awaitable[dict[str, Any]]]
BoundImportNode = Callable[[ImportState], Awaitable[dict[str, Any]]]
# bind_node(name, node, deps) 负责依赖绑定和统一执行边界。
NodeBinder = Callable[[ImportNodeName, ImportNode, object], BoundImportNode]

NORMAL_NODES: tuple[ImportNodeName, ...] = (
    "entry", "save_file", "parse_file", "extract_metadata", "build_records",
    "embedding", "persist", "finish",
)
REQUIRED_NODES: tuple[ImportNodeName, ...] = (*NORMAL_NODES, "handle_error")


def route_after_node(state: ImportState) -> Literal["next", "error"]:
    """所有普通节点（含 finish）先检查错误，只选择一个后继。

    使用 is not None，避免空错误字典被误当作成功。failed 也进入错误分支，
    防止异常摘要遗漏时继续正常链；handle_error 应报告这种合同缺失。
    本函数不捕获抛出的异常，异常转 State 是统一执行绑定器的职责。
    """
    if state["error"] is not None or state["status"] == "failed":
        return "error"
    return "next"


def build_import_graph(
    nodes: Mapping[str, ImportNode],
    *,
    deps: object,
    bind_node: NodeBinder,
) -> "CompiledStateGraph":
    """校验完整节点映射、绑定依赖并 compile；缺少实现时直接报错。

    nodes 必须包含全部九个真实业务节点；本轮不导入不存在的节点文件。
    deps 由调用方管理生命周期，不放入 State；请求级可变上下文按任务隔离。
    bind_node 必须 await node(state, deps=deps)，统一设置阶段、记录日志和进度，
    将普通异常转为 NodeError。已有错误须保留；handle_error 自身失败时由
    执行器/Service 兜底，取消由 Service/finally 清理，不递归进入错误节点。
    公共执行器仍待实现；现有 node_log 不能代替异常转 State 的执行边界。
    """
    expected = set(REQUIRED_NODES)
    supplied = set(nodes)
    missing = expected - supplied
    extra = supplied - expected
    if missing or extra:
        # 不输出函数、依赖对象或 State，避免错误信息包含凭据/资料。
        raise ValueError(
            f"导入图节点映射不完整或存在未知键：missing={sorted(missing)}, "
            f"extra={sorted(str(key) for key in extra)}"
        )
    for name in REQUIRED_NODES:
        if not callable(nodes[name]):
            raise TypeError(f"导入图节点 {name} 必须可调用")
    if not callable(bind_node):
        raise TypeError("bind_node 必须是统一执行绑定器，不能省略异常边界")

    # 延迟导入便于独立检查状态/路由和缺节点错误；真实编译仍必须使用 LangGraph。
    # 不以替身框架或导入失败后的空图冒充编译成功。
    from langgraph.graph import END, START, StateGraph

    # 1. 创建图。
    workflow = StateGraph(ImportState)

    # 2. 注册节点：逐项列出，便于直接核对节点名称和依赖绑定。
    workflow.add_node("entry", bind_node("entry", nodes["entry"], deps))
    workflow.add_node("save_file", bind_node("save_file", nodes["save_file"], deps))
    workflow.add_node("parse_file", bind_node("parse_file", nodes["parse_file"], deps))
    workflow.add_node("extract_metadata", bind_node("extract_metadata", nodes["extract_metadata"], deps))
    workflow.add_node("build_records", bind_node("build_records", nodes["build_records"], deps))
    workflow.add_node("embedding", bind_node("embedding", nodes["embedding"], deps))
    workflow.add_node("persist", bind_node("persist", nodes["persist"], deps))
    workflow.add_node("finish", bind_node("finish", nodes["finish"], deps))
    workflow.add_node("handle_error", bind_node("handle_error", nodes["handle_error"], deps))

    # 3. 入口边。
    workflow.add_edge(START, "entry")

    # 4. 条件边：成功沿主线执行，失败立即进入 handle_error。
    # 同一个节点不再加正常静态边，避免出错时同时执行正常分支。
    workflow.add_conditional_edges(
        "entry", route_after_node,
        path_map={"next": "save_file", "error": "handle_error"},
    )
    workflow.add_conditional_edges(
        "save_file", route_after_node,
        path_map={"next": "parse_file", "error": "handle_error"},
    )
    workflow.add_conditional_edges(
        "parse_file", route_after_node,
        path_map={"next": "extract_metadata", "error": "handle_error"},
    )
    workflow.add_conditional_edges(
        "extract_metadata", route_after_node,
        path_map={"next": "build_records", "error": "handle_error"},
    )
    workflow.add_conditional_edges(
        "build_records", route_after_node,
        path_map={"next": "embedding", "error": "handle_error"},
    )
    workflow.add_conditional_edges(
        "embedding", route_after_node,
        path_map={"next": "persist", "error": "handle_error"},
    )
    workflow.add_conditional_edges(
        "persist", route_after_node,
        path_map={"next": "finish", "error": "handle_error"},
    )
    workflow.add_conditional_edges(
        "finish", route_after_node,
        path_map={"next": END, "error": "handle_error"},
    )

    # 5. 错误处理后结束，不重复进入 handle_error。
    workflow.add_edge("handle_error", END)

    # 6. 编译；不在模块导入时创建全局图。
    return workflow.compile()


if __name__ == "__main__":
    from functools import partial

    from processor.import_processor.state import create_initial_state

    # 只检查骨架合同；没有业务节点，不执行一条伪造的成功导入链。
    state = create_initial_state(task_id="task-check", document_id="doc-check", file_path="check.md")
    assert len(REQUIRED_NODES) == 9 and len(set(REQUIRED_NODES)) == 9
    assert NORMAL_NODES[0] == "entry" and NORMAL_NODES[-1] == "finish"
    assert "handle_error" not in NORMAL_NODES
    assert route_after_node(state) == "next"
    for name in NORMAL_NODES:
        state["current_node"] = name
        state["error"] = {"node": name, "code": "CHECK", "message": "仅自检", "retryable": False}
        assert route_after_node(state) == "error", name
    state["error"] = None
    state["status"] = "failed"
    assert route_after_node(state) == "error"
    state["status"] = "succeeded"
    assert route_after_node(state) == "next"

    def must_not_execute(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("映射校验失败时不应绑定或调用节点")

    invalid_mappings = [
        {},
        {name: must_not_execute for name in NORMAL_NODES},
        {**{name: must_not_execute for name in REQUIRED_NODES}, "unknown": must_not_execute},
    ]
    for mapping in invalid_mappings:
        try:
            build_import_graph(mapping, deps=object(), bind_node=must_not_execute)
        except ValueError as exc:
            assert "missing=" in str(exc) and "extra=" in str(exc)
        else:
            raise AssertionError("缺失/未知节点必须明确报错")
    invalid_node = {name: must_not_execute for name in REQUIRED_NODES}
    invalid_node["entry"] = None
    try:
        build_import_graph(invalid_node, deps=object(), bind_node=must_not_execute)
    except TypeError as exc:
        assert "entry" in str(exc)
    else:
        raise AssertionError("不可调用节点必须明确报错")
    valid_keys = {name: must_not_execute for name in REQUIRED_NODES}
    try:
        build_import_graph(valid_keys, deps=object(), bind_node=None)
    except TypeError as exc:
        assert "bind_node" in str(exc)
    else:
        raise AssertionError("不可调用绑定器必须在导入 LangGraph 前明确报错")
    print("导入图映射校验与纯路由检查通过。")

    # 仅为画图提供测试函数。误执行时明确报错，不返回空结果冒充业务成功。
    async def preview_only_node(state: ImportState, *, deps: object) -> dict[str, Any]:
        raise NotImplementedError("此函数仅用于结构预览，业务节点尚未实现")

    preview_deps = object()
    bindings = []

    def bind_preview_node(name: ImportNodeName, node: ImportNode, deps: object) -> BoundImportNode:
        assert node is preview_only_node and deps is preview_deps
        bindings.append(name)
        return partial(node, deps=deps)

    # 使用真实 LangGraph 编译和画图；不调用 invoke/ainvoke，不导入业务服务。
    preview_app = build_import_graph(
        {name: preview_only_node for name in REQUIRED_NODES},
        deps=preview_deps,
        bind_node=bind_preview_node,
    )
    assert tuple(bindings) == REQUIRED_NODES
    diagram = preview_app.get_graph()
    expected_edges = {("__start__", "entry"), ("handle_error", "__end__")}
    expected_edges.update(zip(NORMAL_NODES, (*NORMAL_NODES[1:], "__end__")))
    expected_edges.update((name, "handle_error") for name in NORMAL_NODES)
    assert {(edge.source, edge.target) for edge in diagram.edges} == expected_edges
    assert all(edge.conditional for edge in diagram.edges if edge.source in NORMAL_NODES)
    print("\n===== 导入图结构预览（业务节点 0/9，未执行业务流程）=====\n")
    diagram.print_ascii()
    print("\n真实编译、依赖绑定及全部边的结构检查通过。")
