"""单场直播录制器：ffmpeg 拉流落盘，断线自动重连，支持按时长分段。

设计要点：
- 输出格式默认 ts：录制中断（断电/杀进程）文件也不损坏
- 默认 ``ffmpeg -c copy`` 纯转封装，CPU 占用几乎为零
- 指定 ``fps`` 时改为 libx264 边录边转码（改帧率只能重新编码）
- 拉流时透传 UA / Referer / 匿名风控 Cookie；登录凭证一律剔除，
  否则抖音会把用户浏览器或手机上的登录态挤下线（提示「账号在别处登录」）
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

from dly.core import compat
from dly.core.naming import build_filename
from dly.core.transcode import normalize_fps
from dly.platforms.base import UA_PC, LiveInfo, strip_login_cookies

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

# ffmpeg 的 HTTP 请求头有长度上限，超了会报 "[http] overlong headers" 直接打不开流。
# 实测抖音的完整 Cookie 有 7~8KB，必须裁剪。
MAX_COOKIE_HEADER = 1500

# 超长 Cookie 时保留的匿名风控字段（流地址自带签名，不带登录凭证也能拉）
_COOKIE_KEEP = (
    "ttwid", "msToken", "odin_tt", "passport_csrf_token",
    "s_v_web_id", "__ac_nonce", "UIFID_TEMP",
)


class RecordingError(RuntimeError):
    """录制失败且没有留下有效产物。"""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class LiveRecorder:
    """对一条直播流执行一次完整录制（直到断流或被停止）。"""

    def __init__(self, ffmpeg_path: str, output_dir: Path,
                 fmt: str = "ts", segment_minutes: float = 0,
                 fps: int = 0,
                 on_progress: Optional[ProgressCallback] = None,
                 segment_seconds: Optional[float] = None):
        self.ffmpeg = ffmpeg_path
        self.output_dir = Path(output_dir)
        self.fmt = (fmt or "ts").lstrip(".").lower()
        # 内部一律按秒：界面可选 s / min / h，分钟只是其中一种来源。
        # 显式传 segment_seconds（哪怕是 0）时以它为准，旧调用方传分钟会自动换算。
        raw = (float(segment_minutes) * 60 if segment_seconds is None
               else segment_seconds)
        try:
            self.segment_seconds = max(0.0, float(raw))
        except (TypeError, ValueError):
            self.segment_seconds = 0.0
        self.fps = normalize_fps(fps)
        self.on_progress = on_progress
        self._proc: Optional[subprocess.Popen] = None
        self._stop_requested = False

    @property
    def segment_minutes(self) -> float:
        """旧调用方仍按分钟读取（CLI / 配置 / 测试）。"""
        return self.segment_seconds / 60

    @property
    def stop_requested(self) -> bool:
        """外部是否请求过停止；自动续录循环据此决定要不要开下一段。"""
        return self._stop_requested

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
        ]
        if self.fps:
            # 目标帧率必须重编码：直播流是固定帧率，-c copy 无法改变
            cmd += [
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-r", str(self.fps),
                "-c:a", "aac", "-b:a", "160k",
            ]
            if self.segment_seconds:
                # 转码时分段按强制关键帧切，避免段边界漂移到下一个自然关键帧
                seconds = _fmt_seconds(self.segment_seconds)
                cmd += ["-force_key_frames",
                        f"expr:gte(t,n_forced*{seconds})"]
        else:
            cmd += ["-c", "copy"]
        if self.segment_seconds:
            cmd += [
                "-f", "segment",
                "-segment_time", _fmt_seconds(self.segment_seconds),
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
        cookie = _trim_cookie(live.cookie, platform=live.platform)
        if cookie:
            lines.append(f"Cookie: {cookie}")
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
            **compat.popen_kwargs(isolate_console=True))
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

        return produced[-1] if self.segment_seconds else outfile

    def stop(self) -> None:
        """外部请求停止录制。"""
        self._stop_requested = True
        if self._proc:
            compat.kill_process_tree(self._proc)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _collect_outputs(self, stem: str) -> List[Path]:
        if self.segment_seconds:
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


def _fmt_seconds(value: float) -> str:
    """ffmpeg 参数里的秒数：整数不带 .0，小数保留有效位，避免科学计数法。"""
    return f"{value:g}"


def _microseconds_to_seconds(value: str) -> float:
    try:
        return int(value) / 1_000_000
    except (TypeError, ValueError):
        return 0.0


def _trim_cookie(cookie: str, limit: int = MAX_COOKIE_HEADER,
                 platform: str = "") -> str:
    """裁剪 Cookie 到 ffmpeg 能接受的体积，并剔除登录凭证。

    实测：完整 Cookie（7~8KB）会让 ffmpeg 报 ``overlong headers`` 直接打不开流；
    而抖音直播 CDN 实际并不校验 Cookie（流地址自带签名），裁剪不影响拉流。
    登录凭证无论 Cookie 长短都要剔除：带进直播请求会被判为「账号在别处登录」，
    挤掉用户在浏览器 / 手机上的会话。
    """
    if not cookie:
        return ""
    if platform == "douyin":
        cookie = strip_login_cookies(cookie)
    if len(cookie) <= limit:
        return cookie

    jar = {}
    for part in cookie.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            jar[key.strip()] = value.strip()

    kept = [f"{k}={jar[k]}" for k in _COOKIE_KEEP if k in jar]
    trimmed = "; ".join(kept)
    if len(trimmed) > limit:
        trimmed = trimmed[:limit].rsplit(";", 1)[0]
    logger.debug("Cookie 已裁剪：%d → %d 字符", len(cookie), len(trimmed))
    return trimmed
