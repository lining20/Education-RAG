"""本地图片定位与 MinIO 资源关联；不创建客户端，不更改包目录或桶权限。"""

from copy import deepcopy
from hashlib import file_digest
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from common.errors import OperationError
from common.logging.logger import logger
from processor.import_processor.state import DataDict
from utils.parsers.common import ParseInput, ParseResult, make_asset, validate_result
from utils.import_cancellation import check_import_cancelled


def resolve_local_image(source: ParseInput, reference: str, result: ParseResult) -> str | None:
    """MD 本地引用只走注入的包解析器；外链保留并告警，不自行下载。"""
    parsed = urlsplit(reference)
    if parsed.scheme in {"http", "https"} or parsed.netloc:
        result.warnings.append({"code": "EXTERNAL_IMAGE_NOT_ARCHIVED", "message": "外链图片保留原引用，未归档"})
        logger.warning("发现外链图片，保留引用但未归档")
        return None
    if source.resource_context is None:
        raise OperationError("MISSING_RESOURCE_CONTEXT", "本地图片引用需要注入文档资源上下文")
    path = source.resource_context.resolve_local(reference)
    asset = make_asset(source, f"md:{reference}", path, reference,
                       mimetypes.guess_type(path)[0] or "application/octet-stream")
    if not any(item.resource_id == asset.resource_id for item in result.resources):
        result.resources.append(asset)
    return asset.resource_id


def archive_resources(result: ParseResult, *, task_id: str, document_id: str,
                      base_prefix: str | None = None,
                      minio_client=None, minio_bucket_name: str | None = None,
                      existing_warnings: list[DataDict] | None = None, stop_event=None) -> DataDict:
    """在解析上下文内调用：校验正文、归档资源、返回独立的块与合并警告。

    传入 base_prefix（文档目录，如 imports/课程介绍.md）时，资源写入
    `{base_prefix}/resources/`，同名替换由 save_file 统一清理，本工具不删除对象。
    任一上传失败直接抛错，不返回部分成功；不发布成功清单。
    只有返回值可并入 State，含临时路径的 ParseResult 不放进 State。
    """
    validate_result(result, require_content=True)
    if result.resources and (minio_client is None or not isinstance(minio_bucket_name, str)
                             or not minio_bucket_name.strip()):
        raise OperationError("MISSING_STORAGE_DEPENDENCY", "保存解析资源需要注入 MinIO 客户端和桶名")
    prefix = base_prefix or f"imports/{task_id}/{document_id}"
    blocks = deepcopy(result.blocks)
    links = {}
    for asset in result.resources:
        check_import_cancelled(stop_event)
        if asset.resource_id in links:
            continue
        path = Path(asset.file_path)
        with path.open("rb") as image:
            fingerprint = file_digest(image, "sha256").hexdigest()
        suffix = mimetypes.guess_extension(asset.media_type) or ".bin"
        object_key = f"{prefix}/resources/{asset.resource_id}-{fingerprint}{suffix}"
        minio_client.fput_object(bucket_name=minio_bucket_name, object_name=object_key,
                                file_path=str(path), content_type=asset.media_type)
        links[asset.resource_id] = {
            "resource_id": asset.resource_id, "object_key": object_key,
            "original_reference": asset.original_reference, "media_type": asset.media_type,
        }
    for block in blocks:
        resource_ids = block["payload"].pop("resource_ids", [])
        if resource_ids:
            block["payload"]["resources"] = [deepcopy(links[resource_id]) for resource_id in resource_ids]
            # 保留原图自己的描述和位置，便于回答模型根据原文上下文选图。
            for resource in block["payload"]["resources"]:
                image = next((item for item in block["payload"].get("images", [])
                              if item.get("resource_id") == resource["resource_id"]), {})
                resource.update(alt=image.get("alt", ""),
                                source_locator=dict(block["source_locator"]),
                                heading_path=list(block["heading_path"]))
        for item in block["payload"].get("image_descriptions", []):
            item["object_key"] = links[item["resource_id"]]["object_key"]
            links[item["resource_id"]]["description"] = item["text"]
            links[item["resource_id"]]["description_origin"] = item["origin"]
            links[item["resource_id"]]["vision_model"] = item["model"]
        # 说明在归档后更新，独立副本也要同步，避免丢失已开启的视觉识别结果。
        for resource in block["payload"].get("resources", []):
            resource.update({key: value for key, value in links[resource["resource_id"]].items()
                             if key in {"description", "description_origin", "vision_model"}})
    logger.info("解析资源归档完成：块数={}，资源数={}", len(blocks), len(links))
    return {"parsed_blocks": blocks,
            "warnings": deepcopy(existing_warnings or []) + deepcopy(result.warnings)}
