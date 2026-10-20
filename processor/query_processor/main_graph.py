"""查询图：按选项并发召回，等待本轮启用的分支结束再融合；错误统一收尾。"""
from importlib import import_module
from langgraph.graph import START, END, StateGraph
from processor.query_processor.state import QueryState
from utils.query_execution import bind_query_node

NODES = ("entry", "load_history", "rewrite", "subject_confirm", "vector_search", "hyde_search",
         "web_search", "join_rrf", "rerank", "answer", "save_history", "handle_error")


def query_nodes():
    return {name: getattr(import_module(f"processor.query_processor.nodes.node_{name}"), "node_" + name) for name in NODES}


def build_query_graph(*, deps):
    graph = StateGraph(QueryState)
    for name, function in query_nodes().items():
        graph.add_node(name, bind_query_node(name, function, deps))
    graph.add_edge(START, "entry")
    successors = {"entry": "load_history", "load_history": "rewrite", "join_rrf": "rerank",
                  "rerank": "answer", "answer": "save_history", "save_history": END}
    def route(state):
        return "error" if state.get("errors") else "next"
    for name, successor in successors.items():
        graph.add_conditional_edges(name, route, {"error": "handle_error", "next": successor})
    graph.add_conditional_edges("rewrite", lambda s: "error" if s.get("errors") else
        "clarify" if s.get("clarification") else "next", {"error": "handle_error", "clarify": "answer", "next": "subject_confirm"})
    def route_retrieval(state):
        # 混合检索必跑；可选召回在路由层决定，不进入未勾选的节点。
        if state.get("errors"):
            return ["handle_error"]
        branches = ["vector_search"]
        if state.get("use_hyde"):
            branches.append("hyde_search")
        if state.get("use_web"):
            branches.append("web_search")
        return branches

    graph.add_conditional_edges("subject_confirm", route_retrieval,
                                ["vector_search", "hyde_search", "web_search", "handle_error"])
    # 三条分支均为单节点，同一 superstep 完成后统一触发下一步融合。
    # 不使用固定三路屏障，避免未启用分支导致融合永远等不到。
    for name in ("vector_search", "hyde_search", "web_search"):
        graph.add_edge(name, "join_rrf")
    graph.add_edge("handle_error", END)
    return graph.compile()
