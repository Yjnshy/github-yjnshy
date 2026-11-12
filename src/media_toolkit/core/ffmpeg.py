"""ffmpeg / ffprobe 探测与调用封装。"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from media_toolkit.core import compat

logger = logging.getLogger(__name__)


def find_ffmpeg(configured: str = "") -> Optional[str]:
    """查找 ffmpeg：配置路径 > PATH > 常见安装位置。"""
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"E:\Program Files (x86)\ytdlp\bin\ffmpeg.exe",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return None


def find_ffprobe(ffmpeg_path: Optional[str], configured: str = "") -> Optional[str]:
    if configured and Path(configured).is_file():
        return configured
    if ffmpeg_path:
        sibling = Path(ffmpeg_path).with_name(
            "ffprobe.exe" if compat.IS_WINDOWS else "ffprobe")
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe")


def probe(path: str, ffprobe_path: Optional[str] = None
          ) -> Optional[Tuple[int, int, float, str]]:
    """读取媒体信息，返回 (宽, 高, 时长秒, 视频编码)；失败返回 None。"""
    ffprobe = ffprobe_path or shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name:format=duration",
             "-of", "default=nw=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=40, **compat.popen_kwargs(),
        ).stdout.decode("utf-8", "ignore")
        info = dict(
            line.split("=", 1) for line in out.splitlines() if "=" in line
        )
        return (int(info.get("width") or 0), int(info.get("height") or 0),
                float(info.get("duration") or 0), info.get("codec_name", ""))
    except Exception as exc:
        logger.debug("ffprobe 失败: %s", exc)
        return None
