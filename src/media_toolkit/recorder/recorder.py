"""单场直播录制器：ffmpeg 拉流落盘，断线自动重连，支持按时长分段。

设计要点：
- 输出格式默认 ts：录制中断（断电/杀进程）文件也不损坏
- ffmpeg -c copy 纯转封装，CPU 占用几乎为零
- 断线后由 monitor 的重连循环负责重新拉起，本类只管「录到断」
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from media_toolkit.core import compat
from media_toolkit.core.naming import build_filename
from media_toolkit.platforms.base import LiveInfo

logger = logging.getLogger(__name__)


class LiveRecorder:
    """对一条直播流执行一次完整录制（直到断流或被停止）。"""

    def __init__(self, ffmpeg_path: str, output_dir: Path,
                 fmt: str = "ts", segment_minutes: int = 0):
        self.ffmpeg = ffmpeg_path
        self.output_dir = output_dir
        self.fmt = fmt.lstrip(".") or "ts"
        self.segment_minutes = max(0, segment_minutes)
        self._proc: Optional[subprocess.Popen] = None

    def record_once(self, live: LiveInfo, part: int = 0) -> Path:
        """录制一场（或一场中的一个分段），返回输出路径。

        ffmpeg 退出即返回（无论正常结束还是断流）。
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stem = build_filename(live.platform, live.anchor, live.title or "直播")
        if part:
            stem += f"_part{part:02d}"
        outfile = self.output_dir / f"{stem}.{self.fmt}"

        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "warning",
            "-nostdin", "-y",
            "-headers", f"Referer: {live.referer}\r\n",
            "-rw_timeout", "15000000",   # 15 秒无数据视为断流
            "-i", live.stream_url,
            "-c", "copy",
        ]
        if self.segment_minutes:
            cmd += ["-f", "segment",
                    "-segment_time", str(self.segment_minutes * 60),
                    "-reset_timestamps", "1",
                    str(self.output_dir / f"{stem}_%03d.{self.fmt}")]
        else:
            cmd.append(str(outfile))

        logger.info("开始录制: %s", outfile)
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **compat.popen_kwargs())
        self._proc.wait()
        logger.info("录制结束（可能断流或下播）: %s", outfile)
        return outfile

    def stop(self) -> None:
        """外部请求停止录制。"""
        if self._proc:
            compat.kill_process_tree(self._proc)
