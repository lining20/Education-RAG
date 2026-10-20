"""同名文件替换：收集旧对象目录，删除旧向量和对象，不连接或创建服务。"""
from common.errors import OperationError
from common.logging.logger import logger
from utils.clients.milvus_collections import collection_names
from utils.clients.milvus_utils import escape_milvus_string
from utils.storage_keys import document_prefix, file_prefix


def remove_previous_file(file_name: str, *, milvus_client, minio_client, bucket_name: str) -> None:
    """四集合按原文件名删除；兼容旧 task/document 对象目录。

    先完整收集旧目录再删除行，避免失去历史目录引用。任何清理失败都中止重传，
    不吞掉异常继续写新版本；这是删除再导入，不能回滚恢复旧版本。
    """
    if milvus_client is None or minio_client is None or not bucket_name:
        raise OperationError("MISSING_STORAGE_DEPENDENCY", "同名替换需要 Milvus 与 MinIO 依赖")
    current_prefix = file_prefix(file_name)
    prefixes = {current_prefix}
    expression = f'source_file_name == "{escape_milvus_string(file_name)}"'
    try:
        # 用迭代器遍历，兼容历史文件对应大量切片的情况。
        for name in collection_names().values():
            iterator = milvus_client.query_iterator(
                collection_name=name, filter=expression,
                output_fields=["source_object_key"], batch_size=500, consistency_level="Strong")
            try:
                while rows := iterator.next():
                    for row in rows:
                        prefix = document_prefix(row.get("source_object_key", ""))
                        # 只允许项目既有的文档目录，不接受 imports 根目录。
                        if prefix and len(prefix.split("/")) >= 2 and prefix != "imports":
                            prefixes.add(prefix)
            finally:
                iterator.close()

        # 先撤销发布凭据，清理中途失败时旧向量也不能继续作为有效资料。
        objects = {}
        for prefix in prefixes:
            objects[prefix] = list(minio_client.list_objects(
                bucket_name=bucket_name, prefix=prefix + "/", recursive=True))
            for item in objects[prefix]:
                if item.object_name.endswith("/manifest.json"):
                    minio_client.remove_object(bucket_name=bucket_name, object_name=item.object_name)
        for name in collection_names().values():
            milvus_client.delete(collection_name=name, filter=expression)
        for items in objects.values():
            for item in items:
                if not item.object_name.endswith("/manifest.json"):
                    minio_client.remove_object(bucket_name=bucket_name, object_name=item.object_name)
        logger.info("同名文件旧记录已清理：file={}，对象目录数={}", file_name, len(prefixes))
    except Exception as exc:
        raise OperationError("FILE_REPLACEMENT_FAILED", "清理同名旧文件失败，未继续上传新内容", retryable=True) from exc
