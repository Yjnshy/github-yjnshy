"""单场直播录制器：ffmpeg 拉流落盘，断线自动重连，支持按时长分段。

设计要点：
- 输出格式默认 ts：录制中断（断电/杀进程）文件也不损坏
- ``ffmpeg -c copy`` 纯转封装，CPU 占用几乎为零
- 拉流时透传 UA / Referer / Cookie：抖音等平台的 CDN 会校验登录态，缺了直接 403
- 录制结束校验落盘体积，避免「静默失败」留下一堆 0 字节文件
- 断线由 monitor 的重连循环负责重新拉起，本类只管「录到断」
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, List, Optional

from media_toolkit.core import compat
from media_toolkit.core.naming import build_filename
from media_toolkit.platforms.base import UA_PC, LiveInfo

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, int], None]

# 低于该体积视为「没录到东西」，通常意味着 403、流地址过期或源站拒绝
MIN_OUTPUT_BYTES = 32 * 1024

# 输出容器 → 分段 muxer 名
_SEGMENT_FORMAT = {
    "ts": "mpegts",
    "mp4": "mp4",
    "mkv": "matroska",
    "flv": "flv",
}


class RecordingError(RuntimeError):
    """录制失败且没有留下有效产物。"""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class LiveRecorder:
    """对一条直播流执行一次完整录制（直到断流或被停止）。"""

    def __init__(self, ffmpeg_path: str, output_dir: Path,
                 fmt: str = "ts", segment_minutes: int = 0,
                 on_progress: Optional[ProgressCallback] = None):
        self.ffmpeg = ffmpeg_path
        self.output_dir = Path(output_dir)
        self.fmt = (fmt or "ts").lstrip(".").lower()
        self.segment_minutes = max(0, int(segment_minutes))
        self.on_progress = on_progress
        self._proc: Optional[subprocess.Popen] = None
        self._stop_requested = False

    # ------------------------------------------------------------------
    # 命令构造（独立成方法，便于单元测试）
    # ------------------------------------------------------------------

    def build_cmd(self, live: LiveInfo, outfile: Path) -> List[str]:
        """拼装 ffmpeg 命令行。"""
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
            "-progress", "pipe:1", "-nostats",
            # 拉流请求上下文
            "-user_agent", live.user_agent or UA_PC,
            "-headers", self._headers(live),
            "-rw_timeout", "15000000",        # 15 秒收不到数据视为断流
            "-reconnect", "1",                # HTTP/FLV 断流自动重连
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", live.stream_url,
            "-c", "copy",
        ]
        if self.segment_minutes:
            cmd += [
                "-f", "segment",
                "-segment_time", str(self.segment_minutes * 60),
                "-segment_format", _SEGMENT_FORMAT.get(self.fmt, self.fmt),
                "-reset_timestamps", "1",
                str(self.output_dir / f"{outfile.stem}_%03d.{self.fmt}"),
            ]
        else:
            cmd.append(str(outfile))
        return cmd

    @staticmethod
    def _headers(live: LiveInfo) -> str:
        """ffmpeg 的 -headers 要求每行以 CRLF 结尾。"""
        lines = [f"Referer: {live.referer or live.room_url}"]
        if live.cookie:
            lines.append(f"Cookie: {live.cookie}")
        return "\r\n".join(lines) + "\r\n"

    # ------------------------------------------------------------------
    # 录制
    # ------------------------------------------------------------------

    def record_once(self, live: LiveInfo, part: int = 0) -> Path:
        """录制一场（或其中一段），返回产出路径。

        ffmpeg 退出即返回（正常下播、断流或外部停止）。
        没有录到有效数据时抛 :class:`RecordingError`。
        """
        if not live.stream_url:
            raise RecordingError("直播流地址为空，无法录制")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        stem = build_filename(live.platform, live.anchor,
                              live.title or "直播")
        if part:
            stem += f"_part{part:02d}"
        outfile = self.output_dir / f"{stem}.{self.fmt}"
        cmd = self.build_cmd(live, outfile)

        self._stop_requested = False
        logger.info("开始录制: %s", outfile.name)

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            **compat.popen_kwargs())
        self._proc = proc

        stderr_tail: Deque[str] = deque(maxlen=30)
        pumps = [
            threading.Thread(target=self._pump_progress, args=(proc.stdout,),
                             daemon=True, name="ffmpeg-progress"),
            threading.Thread(target=self._pump_stderr, args=(proc.stderr, stderr_tail),
                             daemon=True, name="ffmpeg-log"),
        ]
        for t in pumps:
            t.start()

        try:
            # 用轮询代替阻塞 wait()：Ctrl+C 和外部 stop() 都能在 0.5 秒内生效
            while True:
                if proc.poll() is not None:
                    break
                if self._stop_requested:
                    compat.kill_process_tree(proc)
                    proc.wait(timeout=5)
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            # 用户按了 Ctrl+C：必须杀掉 ffmpeg，否则它会变孤儿进程继续写文件
            self._stop_requested = True
            compat.kill_process_tree(proc)
            proc.wait(timeout=5)
            raise
        finally:
            for t in pumps:
                t.join(timeout=3)
            self._proc = None

        produced = self._collect_outputs(stem)
        total = sum(p.stat().st_size for p in produced)
        logger.info("录制结束: %s（%.1f MB）", outfile.name, total / 1024 / 1024)

        if self._stop_requested:
            # 手动停止：即使文件偏小也保留，用户可能只是想抢录一小段
            return produced[-1] if produced else outfile

        if total < MIN_OUTPUT_BYTES:
            for p in produced:
                p.unlink(missing_ok=True)
            raise RecordingError(
                f"未录到有效数据（ffmpeg 退出码 {proc.returncode}，"
                f"产出 {total} 字节）",
                detail="\n".join(list(stderr_tail)[-8:]))

        return produced[-1] if self.segment_minutes else outfile

    def stop(self) -> None:
        """外部请求停止录制。"""
        self._stop_requested = True
        if self._proc:
            compat.kill_process_tree(self._proc)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _collect_outputs(self, stem: str) -> List[Path]:
        if self.segment_minutes:
            return sorted(self.output_dir.glob(f"{stem}_*.{self.fmt}"))
        target = self.output_dir / f"{stem}.{self.fmt}"
        return [target] if target.is_file() else []

    def _pump_progress(self, stream) -> None:
        """解析 ffmpeg -progress 输出，转成（已录秒数, 已录字节）回调。"""
        if stream is None:
            return
        seconds, size = 0.0, 0
        try:
            for raw in stream:
                line = raw.strip()
                if line.startswith("out_time_us="):
                    seconds = _microseconds_to_seconds(line.split("=", 1)[1])
                elif line.startswith("total_size="):
                    try:
                        size = int(line.split("=", 1)[1])
                    except ValueError:
                        size = size
                elif line.startswith("progress=") and self.on_progress:
                    try:
                        self.on_progress(seconds, size)
                    except Exception:  # 回调异常不能拖垮录制
                        logger.debug("进度回调异常", exc_info=True)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _pump_stderr(stream, tail: Deque[str]) -> None:
        if stream is None:
            return
        try:
            for raw in stream:
                line = raw.rstrip()
                if line:
                    tail.append(line)
        except (OSError, ValueError):
            pass


def _microseconds_to_seconds(value: str) -> float:
    try:
        return int(value) / 1_000_000
    except (TypeError, ValueError):
        return 0.0
