"""MinerU 公共上传、轮询、下载工具，PDF 与 Word 复用同一 v4 云端流程。"""
from pathlib import Path
import time
from urllib.parse import urlsplit
import requests
from common.config.mineru_config import MineruConfig
from common.errors import OperationError
from common.logging.logger import logger

def _response_data(response):
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise OperationError("MINERU_SERVICE_FAILED", "MinerU 返回失败或无效响应")
    return payload["data"]


def _http_url(value):
    if not isinstance(value, str) or urlsplit(value).scheme not in {"http", "https"} or not urlsplit(value).netloc:
        raise OperationError("INVALID_MINERU_RESULT", "MinerU 返回的传输地址无效")
    return value


def upload_and_download(part, work_dir: Path, config: MineruConfig, limits, *, stop_event=None) -> Path:
    """申请链接→PUT 本地文件→轮询批次→流式下载；不另行提交解析任务。"""
    from utils.import_cancellation import check_import_cancelled
    check_import_cancelled(stop_event)
    base = config.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {config.api_key}"}
    body = {"files": [{"name": Path(part.file_path).name, "data_id": part.part_id, "is_ocr": True}],
            "model_version": "vlm", "enable_table": True, "enable_formula": True}
    with requests.post(f"{base}/file-urls/batch", headers=headers, json=body, timeout=config.request_timeout) as response:
        data = _response_data(response)
    urls = data.get("file_urls")
    batch_id = data.get("batch_id")
    if (not isinstance(urls, list) or len(urls) != 1 or not isinstance(batch_id, str)
            or not batch_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in batch_id)):
        raise OperationError("INVALID_MINERU_RESULT", "MinerU 未返回有效的单文件上传任务")
    logger.info("MinerU 上传任务已创建：part_id={}，batch_id={}，模型=vlm", part.part_id, batch_id)
    # 签名上传地址不发送 API token，也不设置 Content-Type，直接流式发送文件。
    with open(part.file_path, "rb") as source:
        check_import_cancelled(stop_event)
        with requests.put(_http_url(urls[0]), data=source, timeout=config.request_timeout) as response:
            response.raise_for_status()
    zip_url = None
    for attempt in range(config.max_poll_attempts):
        check_import_cancelled(stop_event)
        with requests.get(f"{base}/extract-results/batch/{batch_id}", headers=headers,
                          timeout=config.request_timeout) as response:
            data = _response_data(response)
        records = data.get("extract_result")
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise OperationError("INVALID_MINERU_RESULT", "MinerU 批次结果与单文件任务不匹配")
        record = records[0]
        if record.get("data_id") != part.part_id and record.get("file_name") != Path(part.file_path).name:
            raise OperationError("INVALID_MINERU_RESULT", "MinerU 结果不属于当前 PDF 分段")
        state = record.get("state")
        logger.info("MinerU 解析进度：part_id={}，状态={}，轮询={}/{}", part.part_id, state, attempt + 1, config.max_poll_attempts)
        if state == "done":
            zip_url = _http_url(record.get("full_zip_url"))
            break
        if state == "failed":
            raise OperationError("MINERU_PARSE_FAILED", "MinerU 分段解析失败，整篇文档不能完成")
        if state not in {"waiting-file", "pending", "running", "converting"}:
            raise OperationError("INVALID_MINERU_RESULT", "MinerU 返回未知任务状态")
        if attempt + 1 < config.max_poll_attempts:
            if stop_event is None:
                time.sleep(config.poll_interval)
            else:
                stop_event.wait(config.poll_interval)
                check_import_cancelled(stop_event)
    if zip_url is None:
        raise OperationError("MINERU_POLL_TIMEOUT", "MinerU 解析轮询超过次数限制", retryable=True)
    archive_path = work_dir / f"{part.part_id}.zip"
    check_import_cancelled(stop_event)
    size = 0
    with requests.get(zip_url, stream=True, timeout=config.request_timeout) as response:
        response.raise_for_status()
        with archive_path.open("xb") as output:
            for block in response.iter_content(64 * 1024):
                check_import_cancelled(stop_event)
                size += len(block)
                if size > limits.result_zip_limits.max_total_bytes:
                    raise OperationError("MINERU_RESULT_TOO_LARGE", "MinerU 结果 ZIP 下载超过大小限制")
                output.write(block)
    return archive_path

