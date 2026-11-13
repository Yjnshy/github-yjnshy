"""ffmpeg / ffprobe 探测与调用封装。"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from media_toolkit.core import compat, paths

logger = logging.getLogger(__name__)

_EXE = ".exe" if compat.IS_WINDOWS else ""


def _search_dirs(configured: str, name: str) -> Optional[str]:
    """按优先级查找可执行文件。

    顺序：配置显式指定 > 程序自带目录（打包时随附）> PATH > 常见安装位置。
    自带的优先级高于 PATH：用户机器上可能装着版本陈旧或残缺的 ffmpeg，
    用随程序分发的版本才有一致的行为。
    """
    if configured and Path(configured).is_file():
        return configured

    for directory in paths.resource_dirs():
        for child in (directory / f"{name}{_EXE}", directory / "bin" / f"{name}{_EXE}"):
            if child.is_file():
                return str(child)

    found = shutil.which(name)
    if found:
        return found

    for c in (rf"C:\ffmpeg\bin\{name}{_EXE}",
              rf"E:\Program Files (x86)\ytdlp\bin\{name}{_EXE}"):
        if Path(c).is_file():
            return c
    return None


def find_ffmpeg(configured: str = "") -> Optional[str]:
    """查找 ffmpeg：配置路径 > 程序自带 > PATH > 常见安装位置。"""
    return _search_dirs(configured, "ffmpeg")


def find_ffprobe(ffmpeg_path: Optional[str], configured: str = "") -> Optional[str]:
    if configured and Path(configured).is_file():
        return configured
    if ffmpeg_path:
        sibling = Path(ffmpeg_path).with_name(f"ffprobe{_EXE}")
        if sibling.is_file():
            return str(sibling)
    return _search_dirs("", "ffprobe")


def probe(path: str, ffprobe_path: Optional[str] = None
          ) -> Optional[Tuple[int, int, float, str]]:
    """读取媒体信息，返回 (宽, 高, 时长秒, 视频编码)；失败返回 None。"""
    ffprobe = ffprobe_path or _search_dirs("", "ffprobe")
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
