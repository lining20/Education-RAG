"""MinIO 对象键约定：文件名是替换边界，同名重传先清旧内容。

    imports/{source_file_name}/
        source.{format}                      原文件
        resources/{resource_id}-{sha256}.{ext}  解析出的图片等资源
        payloads/{record_id}.json            超长完整记录的 JSON
        manifest.json                        发布成功清单

同名任务由 API 串行执行；Milvus 四集合按 source_file_name 清理旧行。
"""

from pathlib import PureWindowsPath
from common.errors import OperationError


def file_prefix(file_name: str) -> str:
    """保留中文文件名；拒绝路径，避免覆盖其他文件的对象前缀。"""
    if (not isinstance(file_name, str) or not file_name or not file_name.isprintable()
            or file_name != file_name.strip() or "/" in file_name or "\\" in file_name
            or ":" in file_name or PureWindowsPath(file_name).name != file_name):
        raise OperationError("INVALID_UPLOAD_METADATA", "来源文件名必须是不含路径的有效文件名")
    return f"imports/{file_name}"


def document_prefix(object_key: str) -> str:
    """从任一类对象键还原文档目录；空值或异常键返回空串，由调用方决定如何报错。"""
    if not isinstance(object_key, str):
        return ""
    object_key = object_key.strip()
    if "/" not in object_key:
        return ""
    prefix = object_key.rsplit("/", 1)[0]
    return prefix if prefix.startswith("imports/") else ""
