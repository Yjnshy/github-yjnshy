"""帧率转换：按目标 FPS 重新编码视频。

两个使用场景：

- **下载后转码**：源视频帧率是固定的（比如 30fps），只有重新编码才能改成
  其他帧率，所以放在下载完成之后做（见 :mod:`media_toolkit.downloader.engine`）。
- **录制时转码**：直播录制直接交给 ffmpeg 边拉边编码，省掉二次处理
  （见 :mod:`media_toolkit.recorder.recorder`）。

约定 ``fps <= 0`` 表示「保持原样」——不转码，直接复制流，CPU 开销为零。
转码是计算密集型操作，界面和命令行都会明确提示耗时。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from media_toolkit.core import compat

logger = logging.getLogger(__name__)

# 界面下拉框提供的档位；0 表示不转码
FPS_CHOICES: tuple = (0, 60, 50, 30, 25, 24, 15)

# 允许的帧率范围（命令行 / 配置文件可以传任意整数）
MIN_FPS, MAX_FPS = 1, 120

# 只有这些容器里的视频才做转码；音频、图片、文案直接跳过
_VIDEO_EXTS = {
    ".mp4", ".mkv", ".ts", ".flv", ".mov", ".avi",
    ".m4v", ".wmv", ".mpg", ".mpeg", ".3gp",
}

# webm 只能装 VP8/VP9，硬塞 H.264 会失败，交给调用方跳过
_SKIP_EXTS = {".webm"}

ProgressCallback = Callable[[str], None]


class TranscodeError(RuntimeError):
    """转码失败（原文件保持不变）。"""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


def normalize_fps(value: object) -> int:
    """把界面 / 命令行传来的值收敛成合法帧率，非法值一律当作「不转码」。"""
    try:
        fps = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return fps if MIN_FPS <= fps <= MAX_FPS else 0


def fps_label(fps: int) -> str:
    return "原帧率" if not fps else f"{fps}fps"


def is_transcodable(path: Path) -> bool:
    suffix = Path(path).suffix.lower()
    return suffix in _VIDEO_EXTS and suffix not in _SKIP_EXTS


# ----------------------------------------------------------------------
# 探测
# ----------------------------------------------------------------------

def probe_fps(ffprobe_path: Optional[str], path: Path) -> float:
    """读取视频流帧率（读不到返回 0）。"""
    if not ffprobe_path:
        return 0.0
    try:
        out = subprocess.run(
            [ffprobe_path, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of",
             "default=nw=1:nk=1", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30, **compat.popen_kwargs(),
        ).stdout.decode("utf-8", "ignore").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("读取帧率失败 %s: %s", path, exc)
        return 0.0

    if "/" in out:
        num, _, den = out.partition("/")
        try:
            denominator = float(den) or 1.0
            return float(num) / denominator
        except ValueError:
            return 0.0
    try:
        return float(out)
    except ValueError:
        return 0.0


# ----------------------------------------------------------------------
# 转码
# ----------------------------------------------------------------------

def build_cmd(ffmpeg: str, src: Path, dst: Path, fps: int,
              crf: int = 23, preset: str = "veryfast",
              gop_seconds: float = 0.0) -> List[str]:
    """拼装转码命令（独立成函数，便于单元测试）。

    ``crf 23`` 是 H.264 的视觉无损常用值：手机短视频源码率普遍不高，
    再往低调（数字越小画质越高）只会让体积翻倍，肉眼几乎看不出差别。
    """
    src, dst = Path(src), Path(dst)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
        "-i", str(src),
        # 只取第一条视频 + 可选音频：转码会丢弃多音轨与字幕，避免意外
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",            # 兼容老播放器与手机
        "-r", str(fps),
    ]
    if gop_seconds > 0:
        cmd += ["-g", str(max(1, int(round(fps * gop_seconds))))]
    cmd += ["-c:a", "aac", "-b:a", "192k"]
    if dst.suffix.lower() in (".mp4", ".m4v", ".mov"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(dst))
    return cmd


def transcode(ffmpeg: str, src: Path, fps: int,
              keep_original: bool = False,
              ffprobe_path: Optional[str] = None,
              notify: Optional[ProgressCallback] = None
              ) -> Optional[Path]:
    """把 ``src`` 重编码到目标帧率，成功返回文件路径。

    - 默认原地替换，文件名保持「平台_作者_标题_时间戳」不变
    - ``keep_original=True`` 时把原片保留为 ``xxx_原画.ext``
    - 失败抛 :class:`TranscodeError`，原文件不受影响
    """
    src = Path(src)
    fps = normalize_fps(fps)
    if not fps or not src.is_file():
        return None
    if not is_transcodable(src):
        logger.debug("跳过转码（容器不支持）: %s", src.name)
        return None

    say = notify or (lambda msg: None)
    before = probe_fps(ffprobe_path, src)
    if before and abs(before - fps) < 0.01:
        say(f"源视频已是 {fps}fps，无需转码")
        return src

    tmp = src.with_name(f"{src.stem}.__fps_tmp{src.suffix}")
    tmp.unlink(missing_ok=True)
    say(f"正在转码到 {fps}fps（重新编码较慢，请耐心等待）…")

    try:
        proc = subprocess.run(
            build_cmd(ffmpeg, src, tmp, fps),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            **compat.popen_kwargs(isolate_console=True),
        )
    except OSError as exc:
        raise TranscodeError(f"无法调用 ffmpeg：{exc}") from exc

    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise TranscodeError(
            f"转码失败（ffmpeg 退出码 {proc.returncode}）",
            detail="\n".join((proc.stderr or "").strip().splitlines()[-8:]),
        )

    try:
        if keep_original:
            backup = src.with_name(f"{src.stem}_原画{src.suffix}")
            backup.unlink(missing_ok=True)
            src.replace(backup)
            tmp.replace(src)
            say(f"已保留原片：{backup.name}")
        else:
            src.unlink()
            tmp.replace(src)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise TranscodeError(f"替换文件失败：{exc}") from exc

    say(f"✓ 转码完成：{src.name}（{fps}fps）")
    return src
