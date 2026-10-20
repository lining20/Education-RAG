"""单文档导入图：文件格式分支汇入公共链，失败统一收尾。"""
from langgraph.graph import END, START, StateGraph
from processor.import_processor.state import ImportState
from utils.node_execution import ImportDependencies, bind_node

NORMAL_NODES = (
    "entry", "save_file", "pdf_parse", "word_parse", "md_img", "document_split",
    "metadata_recognition", "bge_embedding", "import_milvus", "finish",
)
REQUIRED_NODES = (*NORMAL_NODES, "handle_error")


def route_after_node(state: ImportState) -> str:
    return "error" if state["error"] is not None or state["status"] == "failed" else "next"


def route_format(state: ImportState) -> str:
    return "error" if route_after_node(state) == "error" else state["source_file"]["format"]


def import_nodes() -> dict:
    """延迟装配真实节点，不在模块导入时连接服务或加载模型。"""
    from processor.import_processor.nodes.node_entry import node_entry
    from processor.import_processor.nodes.node_save_file import node_save_file
    from processor.import_processor.nodes.node_pdf_parse import node_pdf_parse
    from processor.import_processor.nodes.node_word_parse import node_word_parse
    from processor.import_processor.nodes.node_md_img import node_md_img
    from processor.import_processor.nodes.node_document_split import node_document_split
    from processor.import_processor.nodes.node_metadata_recognition import node_metadata_recognition
    from processor.import_processor.nodes.node_bge_embedding import node_bge_embedding
    from processor.import_processor.nodes.node_import_milvus import node_import_milvus
    from processor.import_processor.nodes.node_finish import node_finish
    from processor.import_processor.nodes.node_handle_error import node_handle_error
    functions = locals()
    return {name: functions[f"node_{name}"] for name in REQUIRED_NODES}


def build_import_graph(*, deps: ImportDependencies):
    # 1. 创建图，绑定每篇文档独立的运行依赖。
    graph = StateGraph(ImportState)
    for name, node in import_nodes().items():
        graph.add_node(name, bind_node(name, node, deps))
    graph.add_edge(START, "entry")
    # 2. 归档后按格式路由；MD 直接进入公共链。
    graph.add_conditional_edges("save_file", route_format, {
        "md": "md_img", "pdf": "pdf_parse", "docx": "word_parse", "error": "handle_error",
    })
    # 3. 每个业务节点只选择一个后继。
    successors = {
        "entry": "save_file", "pdf_parse": "md_img", "word_parse": "md_img",
        "md_img": "document_split", "document_split": "metadata_recognition",
        "metadata_recognition": "bge_embedding", "bge_embedding": "import_milvus",
        "import_milvus": "finish", "finish": END,
    }
    for name, successor in successors.items():
        graph.add_conditional_edges(name, route_after_node, {"next": successor, "error": "handle_error"})
    # 4. 错误收尾后结束。
    graph.add_edge("handle_error", END)
    return graph.compile()
