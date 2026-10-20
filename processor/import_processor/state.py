"""导入图的数据合同与初始状态；不创建任务、客户端或模型。

TypedDict 只提供类型提示；业务校验由对应节点及公共工具执行。
"""

from copy import deepcopy
from typing import Any, Literal, TypedDict


ImportStatus = Literal["pending", "running", "succeeded", "failed"]
ImportNodeName = Literal[
    "entry", "save_file", "pdf_parse", "word_parse", "md_img", "document_split",
    "metadata_recognition", "bge_embedding", "import_milvus", "finish", "handle_error",
]
CollectionKey = Literal["document_index", "courses", "document_chunks", "questions"]
ContentType = Literal["course_intro", "document_chunk", "project_material", "question"]
# 元数据、定位和业务 payload 的细节随对应节点完善；这里只允许可序列化数据，
# 不放 SDK 对象、异常、文件句柄、队列或取消信号。Any 不代表免除节点校验。
DataDict = dict[str, Any]


class SourceFile(TypedDict):
    """原文件归档信息；object_key 为持久对象键，不是临时签名链接。"""

    document_id: str
    file_name: str
    object_key: str
    format: Literal["md", "docx", "pdf"]
    fingerprint: str
    import_manifest_key: str  # 任务级成功清单对象键；finish 最后写入，查询校验此清单。


class ParsedBlock(TypedDict):
    """按阅读顺序排列的内容块；表格、代码、图片关联信息保留在 payload。"""

    block_id: str
    text: str
    block_type: Literal["heading", "paragraph", "table", "code", "image_ref"]
    heading_path: list[str]
    source_locator: DataDict
    payload: DataDict


class ContentRecord(TypedDict):
    """完整内容中间对象；投影到 Milvus.record_payload，超长内容转为 MinIO JSON。"""

    record_id: str
    task_id: str
    document_id: str
    content_type: ContentType
    content: str
    metadata: DataDict
    payload: DataDict
    source: DataDict


class ChunkDraft(TypedDict):
    """fields 仅含目标集合的字段，不含向量；字段由内容构建器投影。"""

    collection_key: CollectionKey
    fields: DataDict


class EmbeddedBatches(TypedDict):
    """四集合的待 upsert 行；每行含向量，空集合使用 []。"""

    document_index: list[DataDict]
    courses: list[DataDict]
    document_chunks: list[DataDict]
    questions: list[DataDict]


class NodeError(TypedDict):
    """可序列化的原始错误摘要；不保存异常对象或完整堆栈。"""

    node: ImportNodeName
    code: str
    message: str
    retryable: bool


class CollectionCounts(TypedDict):
    document_index: int
    courses: int
    document_chunks: int
    questions: int


class RecordCounts(TypedDict):
    # course_intro 按系列/子课程分开计数，二者合计才是课程完整记录数。
    series: int
    course: int
    document_chunk: int
    project_material: int
    question: int


class ImportCounts(TypedDict):
    """累计统计；切片、向量与实际入库数量分别记录，不以计划数冒充实写数。"""

    files: int  # entry 确认有效后置 1；本图一次只处理一个文件。
    records: int
    rejected_records: int
    records_by_type: RecordCounts
    chunks_by_collection: CollectionCounts
    embedded_by_collection: CollectionCounts
    persisted_records: int
    persisted_by_collection: CollectionCounts


class ImportState(TypedDict):
    """一次导入任务的内存状态；使用 create_initial_state 创建完整初值。

    节点只返回本次修改字段的增量字典，不原地改 State。当前串行图采用
    默认覆盖语义，warnings/统计等需要返回更新后的完整字段值，不自动追加。
    内存任务表只保留统计和产物引用，不复制整份 State 或全部向量。
    """

    # Service 已在内存中创建 pending 任务和临时文件；entry 再次校验，不在这里生成 ID。
    task_id: str
    document_id: str
    file_path: str
    upload_metadata: DataDict
    task_claimed: bool  # 只有领取成功的调用可在失败时清理本任务产物。

    source_file: SourceFile | None  # save_file 成功前为 None，不伪造归档信息。
    parsed_blocks: list[ParsedBlock]
    document_metadata: DataDict
    typed_blocks: list[DataDict]
    records: list[ContentRecord]
    index_drafts: list[DataDict]
    chunks: list[ChunkDraft]
    embedded_records: EmbeddedBatches
    # persist 负责填充实写 ID/数量明细；失败清理仍以 task_id 核对数据库残留，
    # 不能假设抛异常前尚未提交到 State 的批次明细一定存在。
    persist_result: DataDict
    counts: ImportCounts
    warnings: list[DataDict]
    rejected_records: list[DataDict]

    status: ImportStatus
    current_node: ImportNodeName | Literal[""]  # 尚未执行任何节点时为空串。
    error: NodeError | None  # 首个业务错误保留；清理错误另记 warnings/result。
    result: DataDict  # finish/handle_error 组织；空字典不表示成功。


def create_initial_state(
    *, task_id: str, document_id: str, file_path: str,
    upload_metadata: DataDict | None = None,
) -> ImportState:
    """创建独立初值，不做文件 I/O、任务落库或业务校验。

    输入标识/路径不作静默修正，交 entry 校验；必填参数没有虚构默认值。
    可选字符串未知用空串、数值未知用 None、数组/定位未知用 []/{} 的
    业务约定由后续构建器保持。这里的计数 0 表示尚未处理，而非已成功。
    """
    return {
        "task_id": task_id,
        "document_id": document_id,
        "file_path": file_path,
        "upload_metadata": deepcopy(upload_metadata) if upload_metadata is not None else {},
        "task_claimed": False,
        "source_file": None,
        "parsed_blocks": [],
        "document_metadata": {},
        "typed_blocks": [],
        "records": [],
        "index_drafts": [],
        "chunks": [],
        "embedded_records": {"document_index": [], "courses": [], "document_chunks": [], "questions": []},
        "persist_result": {},
        "counts": {
            "files": 0, "records": 0, "rejected_records": 0,
            "records_by_type": {
                "series": 0, "course": 0, "document_chunk": 0,
                "project_material": 0, "question": 0,
            },
            "chunks_by_collection": {"document_index": 0, "courses": 0, "document_chunks": 0, "questions": 0},
            "embedded_by_collection": {"document_index": 0, "courses": 0, "document_chunks": 0, "questions": 0},
            "persisted_records": 0,
            "persisted_by_collection": {"document_index": 0, "courses": 0, "document_chunks": 0, "questions": 0},
        },
        "warnings": [],
        "rejected_records": [],
        "status": "pending",
        "current_node": "",
        "error": None,
        "result": {},
    }
