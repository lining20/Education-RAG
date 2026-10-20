"""视觉请求的图片格式适配；只转换请求数据，不改动原图和归档指纹。"""
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from PIL import Image

from common.errors import OperationError
from common.logging.logger import logger
from utils.parsers.common import ResourceAsset

_DIRECT_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg",
                   "WEBP": "image/webp", "GIF": "image/gif"}


def _render_metafile(path: Path) -> bytes:
    """无 Windows 原生渲染器时调用 Inkscape；Linux 部署需安装该系统依赖。"""
    executable = shutil.which("inkscape")
    if not executable:
        raise OperationError("MISSING_METAFILE_RENDERER",
            "当前环境无法渲染 EMF/WMF，请在服务器安装 Inkscape 并加入 PATH，重启服务后重试")
    with TemporaryDirectory(prefix="vision_metafile_") as directory:
        output = Path(directory) / "image.png"
        try:
            subprocess.run([executable, str(path.resolve()), "--export-type=png",
                            f"--export-filename={output}", "--export-background=white",
                            "--export-background-opacity=1"],
                           check=True, capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            # 确认产物是真实可解码 PNG，不通过修改后缀冒充格式转换。
            with Image.open(output) as image:
                if image.format != "PNG":
                    raise ValueError("not PNG")
                image.load()
            return output.read_bytes()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise OperationError("IMAGE_CONVERSION_FAILED", "EMF/WMF 转 PNG 失败，请检查图片及 Inkscape 渲染环境") from exc


def prepare_vision_image(asset: ResourceAsset) -> tuple[bytes, str]:
    """按实际文件内容识别格式：支持格式原样发送，其余可解码图片转 PNG。

    Pillow 在 Windows 可渲染 WMF/EMF，其他平台回退到 Inkscape。
    BMP、TIFF 等使用 Pillow；TIFF/GIF 等多帧资源仅取第一帧进行本次识别。
    """
    path = Path(asset.file_path)
    try:
        with Image.open(path) as image:
            detected = image.format
            if detected in _DIRECT_FORMATS:
                return path.read_bytes(), _DIRECT_FORMATS[detected]
            if detected == "WMF" and not hasattr(Image.core, "drawwmf"):
                data = _render_metafile(path)
            else:
                try:
                    image.load()
                    buffer = BytesIO()
                    image.convert("RGBA").save(buffer, format="PNG")
                    data = buffer.getvalue()
                except (OSError, ValueError):
                    if detected != "WMF":
                        raise
                    # Windows 原生渲染失败时，也允许使用已安装的 Inkscape。
                    logger.warning("原生 EMF/WMF 渲染失败，尝试 Inkscape：resource_id={}", asset.resource_id)
                    data = _render_metafile(path)
        logger.info("视觉图片已转 PNG：resource_id={}，原格式={}，原图保持不变", asset.resource_id, detected)
        return data, "image/png"
    except OperationError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise OperationError("IMAGE_DECODE_FAILED", "图片无法解码或转换，请检查原图是否损坏或格式是否受支持") from exc
