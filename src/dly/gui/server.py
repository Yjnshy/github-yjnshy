"""图形界面：内置 HTTP 服务 + 单页前端。

窗口形态有两种，共用同一套界面与接口：

- **桌面窗口**（默认）：由 :mod:`dly.gui.desktop` 用 pywebview 承载
- **浏览器**：``dly gui --web``，调试或没有 WebView2 时使用

为什么界面用网页技术：Tkinter 依赖 tcl/tk，部分精简版 Python 不带（本项目
实测环境就没有），而 Web 界面用标准库 http.server 即可，跨平台一致，
后续做手机端也能直接复用同一套前端。

安全约定：只绑定 127.0.0.1，端口固定范围内递增；不对局域网暴露。
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:                       # 非 Windows 跑测试时不应该炸
    import winreg
except ImportError:        # pragma: no cover
    winreg = None  # type: ignore[assignment]

from dly import __version__
from dly.core.compat import open_folder
from dly.core.config import (config_file_path, load_config,
                             save_settings, settings_from_config)
from dly.core.ffmpeg import find_ffmpeg
from dly.core.logging import setup_logging
from dly.core.transcode import FPS_CHOICES, normalize_fps

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_RANGE = 20
MAX_EVENTS = 1200

# 录制时长的单位换算：界面可填 30 s / 30 min / 1 h，内部一律按秒交给 ffmpeg
SEGMENT_UNITS = {"s": 1.0, "min": 60.0, "h": 3600.0}
MIN_SEGMENT_SECONDS = 1.0
MAX_SEGMENT_SECONDS = 24 * 3600

# 自动续录时，连续这么多段都没录到有效数据就收手，避免无限重连刷文件
MAX_CONTINUOUS_FAILURES = 3

# 选择文件夹是模态窗口，同一时刻只允许一个（连点按钮不再叠出第二个）
_PICK_LOCK = threading.Lock()


def parse_segment_seconds(data: Dict[str, Any]) -> float:
    """把界面的「数值 + 单位」换算成秒。

    新前端传 ``segment_value`` / ``segment_unit``；旧前端只传 ``segment``
    （单位固定为分钟），两条路都要认，避免老页面或脚本突然失效。
    """
    if "segment_value" in data or "segment_unit" in data:
        factor = SEGMENT_UNITS.get(
            str(data.get("segment_unit") or "min").lower(), 60.0)
        raw = data.get("segment_value", data.get("segment", 0))
    else:
        factor, raw = 60.0, data.get("segment", 0)
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0                      # 0 或空 = 不限时，录到手动停止
    seconds = max(MIN_SEGMENT_SECONDS, value * factor)
    return min(seconds, MAX_SEGMENT_SECONDS)


class AppState:
    """界面共享状态：日志事件流 + 各任务状态。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: List[Dict[str, Any]] = []
        self._next_id = 1
        self.download = {"active": False}
        self.watch = {"active": False, "rooms": 0}
        self.record = {
            "active": False,
            "status": "空闲中",
            "level": "idle",
            "seconds": 0.0,       # ffmpeg 上报的内容时长（可能跳变）
            "size": 0,
            "part": 0,            # 当前第几段
            "duration": 0.0,      # 本次设定的录制时长（秒，0 = 不限时）
            "auto_next": False,   # 到点后是否自动续录
            "started_at": None,   # 当前段开始时刻（time.monotonic）
            "ended_at": None,     # 当前段结束时刻
        }
        self.recorder = None
        self.monitor = None
        self.desktop = False          # 桌面窗口模式下关窗即退出，前端不显示浏览器模式的退出提示
        self.last_request = time.monotonic()   # 最近一次收到 HTTP 请求的时刻（浏览器模式空闲自退用）
        self._quit = threading.Event()
        # 独立于 recorder 内部状态的停止信号：自动续录的探测间隙里也要能停下
        self.record_stop = threading.Event()

    # ---- 事件流 ----

    def log(self, tab: str, text: str, level: str = "info") -> None:
        with self._lock:
            self._events.append({
                "id": self._next_id,
                "tab": tab,
                "text": str(text),
                "level": level,
            })
            self._next_id += 1
            if len(self._events) > MAX_EVENTS:
                del self._events[: len(self._events) - MAX_EVENTS // 2]

    def events_since(self, after: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["id"] > after]

    # ---- 录制状态 ----

    def set_record(self, **kwargs: Any) -> None:
        with self._lock:
            self.record.update(kwargs)

    def record_snapshot(self) -> Dict[str, Any]:
        """给前端的录制状态：额外算出按墙钟平滑推进的 ``elapsed``。

        ffmpeg 的 ``out_time_us`` 跟着直播流时间戳走，遇到缓冲、重连或
        时间戳不连续会一次跳 1~2 秒；界面显示改用墙钟，每 0.3 秒轮询
        就能稳定地一秒一秒走，也和「录满 N 秒自动停」的口径一致。
        ``seconds`` 仍保留 ffmpeg 上报的内容时长，供日志与其它调用方使用。
        """
        with self._lock:
            rec = dict(self.record)
        start = rec.pop("started_at", None)
        ended = rec.pop("ended_at", None)
        if start is not None:
            stop = ended if ended is not None else time.monotonic()
            rec["elapsed"] = max(0.0, stop - start)
        else:
            rec["elapsed"] = rec.get("seconds", 0.0)
        return rec

    # ---- 退出 ----

    def request_quit(self) -> None:
        self._quit.set()

    def busy(self) -> bool:
        """有没有下载 / 录制 / 值守任务正在跑（空闲自退时要看）。"""
        return bool(self.download.get("active") or self.watch.get("active")
                    or self.record.get("active"))

    @property
    def quit_requested(self) -> bool:
        return self._quit.is_set()

    def active_tab(self) -> str:
        if self.record.get("active"):
            return "record"
        if self.watch.get("active"):
            return "watch"
        return "download"


class _LogBridge(logging.Handler):
    """把模块日志转发到界面日志区。"""

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.state.log(self.state.active_tab(), self.format(record))
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------
# 业务动作（全部在后台线程执行）
# ----------------------------------------------------------------------

def _run_download(state: AppState, text: str, quality: str, outdir: str,
                  fps: int = 0, keep_original: bool = False) -> None:
    from dly.downloader.engine import DownloadEngine

    with state._lock:  # noqa: SLF001 内部状态，避免并发重复启动
        state.download["active"] = True
    state.log("download", "开始解析链接…")
    if fps:
        state.log("download", f"下载完成后转码到 {fps}fps（需重新编码，比较耗时）")

    # yt-dlp 的进度回调非常密集，界面上按 1.5 秒节流，否则日志会被刷屏
    last = {"at": 0.0}

    def progress(message: str) -> None:
        if "下载中" in message or "%" in message:
            now = time.monotonic()
            if now - last["at"] < 1.5:
                return
            last["at"] = now
        state.log("download", message)

    try:
        config = load_config()
        config.quality = quality
        config.keep_original = bool(keep_original)
        engine = DownloadEngine(config, progress=progress)
        ok, total = engine.download_text(text, quality=quality,
                                         output_dir=outdir, fps=fps)
        level = "ok" if ok == total and total else "warn"
        state.log("download", f"完成：成功 {ok} / 共 {total}", level)
    except Exception as exc:  # noqa: BLE001
        logger.exception("下载失败")
        state.log("download", f"下载失败：{exc}", "error")
    finally:
        state.download["active"] = False


def probe_live(url: str) -> Dict[str, Any]:
    """查询直播间标题 / 主播 / 开播状态，供界面自动回填核对。"""
    from dly.cookies.manager import CookieManager
    from dly.platforms import find_adapter

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"ok": False, "error": "链接格式不对，请以 http(s):// 开头"}
    config = load_config()
    adapter = find_adapter(url, CookieManager(config.cookies),
                           proxy=config.proxy)
    if adapter is None:
        return {"ok": False, "error": "无法识别该链接"}
    live = adapter.get_live_info(url)
    if live is None:
        return {"ok": False,
                "error": "暂时取不到直播间信息（可能是风控或网络异常）"}
    return {
        "ok": True,
        "title": live.title or "",
        "anchor": live.anchor or "",
        "is_live": bool(live.is_live),
        "platform": live.platform or "",
        "has_stream": bool(live.stream_url),
    }


def _run_record(state: AppState, url: str, outroot: str,
                segment: float = 0, fps: int = 0,
                auto_next: bool = True) -> None:
    """录制一个直播间。

    ``segment`` 是界面填写的**录制时长**（秒，0 = 不限时、一直录到手动停止）。
    录满后 ffmpeg 会自行退出，因此「录完一段」不再只意味着断流：
    ``auto_next`` 打开时重新探测，仍在开播就接着录下一段（文件名带
    ``_part02``）；关闭时录完这一段就结束。
    """
    from dly.cookies.manager import CookieManager
    from dly.platforms import find_adapter
    from dly.recorder.recorder import LiveRecorder, RecordingError

    state.set_record(active=True, status="正在获取直播流…", level="idle",
                     seconds=0.0, size=0, part=0, duration=segment,
                     auto_next=auto_next, started_at=None, ended_at=None)
    try:
        config = load_config()
        adapter = find_adapter(url, CookieManager(config.cookies),
                               proxy=config.proxy)
        if adapter is None:
            state.log("record", "无法识别的直播间链接", "error")
            state.set_record(status="无法识别链接", level="error")
            return

        live = adapter.get_live_info(url)
        if not live or not live.is_live or not live.stream_url:
            state.log("record", "当前未开播，或取不到流地址", "warn")
            state.set_record(status="未开播", level="warn")
            return

        ffmpeg = find_ffmpeg(config.ffmpeg_path)
        if not ffmpeg:
            state.log("record", "未找到 ffmpeg，无法录制", "error")
            state.set_record(status="缺少 ffmpeg", level="error")
            return

        outdir = Path(outroot or config.output_dir) / "直播录制"
        recorder = LiveRecorder(
            ffmpeg, outdir,
            fmt=config.record.format,
            # 界面填的时长是「录多久就停」，交给 -t 让 ffmpeg 到点自行退出。
            # 不传 segment_seconds：那是无限切文件的 muxer，永远不会自己退出。
            max_duration=segment,
            fps=fps,
            on_progress=lambda s, b: state.set_record(seconds=s, size=b),
        )
        state.recorder = recorder
        state.set_record(status=f"录制中 · {live.anchor}", level="live")
        state.log("record", f"主播：{live.anchor}", "ok")
        state.log("record", f"标题：{live.title}")
        state.log("record", f"输出目录：{outdir}")
        if fps:
            state.log("record", f"目标帧率：{fps}fps（边录边转码，CPU 占用较高）", "warn")
        else:
            state.log("record", "目标帧率：原画直通（不转码）")
        state.log("record", f"录制时长：{segment:g} 秒（到点自动停止）" if segment
                  else "录制时长：不限（一直录到手动点停止）")
        state.log("record", "自动续录：开（到点后仍在开播就接着录下一段）"
                  if auto_next else "自动续录：关（录完这一段即结束）")

        part = 0
        failures = 0
        while not state.record_stop.is_set():
            part += 1
            # 每段单独计时：探测直播间的时间不算进「已录时长」，
            # 续录开新文件时显示也能从第 0 秒重新开始。
            state.set_record(part=part, seconds=0.0, size=0,
                             started_at=time.monotonic(), ended_at=None)
            try:
                path = recorder.record_once(live, part=part if part > 1 else 0)
                state.set_record(ended_at=time.monotonic())
                state.log("record", f"已保存：{path}", "ok")
                failures = 0
            except RecordingError as exc:
                state.set_record(ended_at=time.monotonic())
                failures += 1
                state.log("record", f"录制失败：{exc}", "error")
                if exc.detail:
                    state.log("record", exc.detail, "muted")
                if not auto_next or failures >= MAX_CONTINUOUS_FAILURES:
                    state.set_record(status="录制失败", level="error")
                    break

            if state.record_stop.is_set() or recorder.stop_requested:
                state.set_record(status="已停止", level="idle")
                break
            if not auto_next:
                state.log("record", "已录完这一段，自动停止（未开启自动续录）")
                state.set_record(status="已完成", level="idle")
                break

            # ffmpeg 退出 = 断流或下播；重新探测才知道该不该接下一段
            state.set_record(status="正在准备下一段…", level="idle")
            state.log("record", "正在检查直播间状态…")
            try:
                fresh = adapter.get_live_info(url)
            except Exception as exc:  # noqa: BLE001 探测失败按「已结束」处理
                logger.warning("续录前探测失败：%s", exc)
                fresh = None
            if state.record_stop.is_set():
                state.set_record(status="已停止", level="idle")
                break
            if not fresh or not fresh.is_live or not fresh.stream_url:
                state.log("record", "直播已结束或暂时取不到流，停止自动续录", "warn")
                state.set_record(status="已停止", level="idle")
                break
            live = fresh
            state.log("record", f"自动续录：开始第 {part + 1} 段", "ok")
            state.set_record(status=f"录制中 · {live.anchor}（第 {part + 1} 段）",
                             level="live", seconds=0.0, size=0)
    except RecordingError as exc:
        state.log("record", f"录制失败：{exc}", "error")
        if exc.detail:
            state.log("record", exc.detail, "muted")
        state.set_record(status="录制失败", level="error")
    except Exception as exc:  # noqa: BLE001
        logger.exception("录制异常")
        state.log("record", f"录制异常：{exc}", "error")
        state.set_record(status="录制异常", level="error")
    finally:
        state.recorder = None
        state.record_stop.clear()
        state.set_record(active=False, ended_at=time.monotonic())


def _run_watch(state: AppState, rooms: List[str], interval: int,
               fps: int = 0) -> None:
    from dly.recorder.monitor import LiveMonitor

    with state._lock:
        state.watch = {"active": True, "rooms": len(rooms)}
    try:
        config = load_config()
        config.record.poll_interval = interval
        config.record.fps = normalize_fps(fps)
        if config.record.fps:
            state.log("watch",
                      f"值守目标帧率：{config.record.fps}fps（每场都会转码录制）",
                      "warn")
        monitor = LiveMonitor(config,
                              notify=lambda m: state.log("watch", m))
        state.monitor = monitor
        monitor.watch(rooms)
    except Exception as exc:  # noqa: BLE001
        logger.exception("值守异常")
        state.log("watch", f"值守异常：{exc}", "error")
    finally:
        state.monitor = None
        with state._lock:
            state.watch = {"active": False, "rooms": 0}


def _run_cookie_check(state: AppState, browser: str) -> None:
    from dly.cookies.manager import CookieManager

    state.log("download", f"正在从 {browser} 读取 Cookie…")
    try:
        config = load_config()
        config.cookies.browser = browser
        manager = CookieManager(config.cookies, use_cache=False)
        for platform in ("douyin", "bilibili", "xiaohongshu"):
            header = manager.get_header(platform)
            if header:
                note = "含登录态" if "sessionid" in header else "无登录态"
                state.log("download",
                          f"{platform}：成功（{len(header)} 字符，{note}）", "ok")
            else:
                state.log("download", f"{platform}：未取到", "warn")
                if manager.last_error:
                    state.log("download", f"  原因：{manager.last_error}", "warn")
        manager.cleanup()
    except Exception as exc:  # noqa: BLE001
        state.log("download", f"Cookie 检测异常：{exc}", "error")


# ----------------------------------------------------------------------
# HTTP 层
# ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    state: AppState
    server_version = "DLY"
    protocol_version = "HTTP/1.1"

    # 静音访问日志，避免刷控制台
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    # ---- 工具 ----

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _snapshot(self) -> Dict[str, Any]:
        config = load_config()
        return {
            "version": __version__,
            "default_output": str(Path(config.output_dir).resolve()),
            "quality": config.quality,
            "fps": config.fps,
            "record_fps": config.record.fps,
            "keep_original": config.keep_original,
            "fps_choices": list(FPS_CHOICES),
            "segment_minutes": config.record.segment_minutes,
            "poll_interval": config.record.poll_interval,
            "browser": config.cookies.browser,
            "rooms": config.record.rooms,
            "settings": settings_from_config(config),
            "ffmpeg": bool(find_ffmpeg(config.ffmpeg_path)),
            "desktop": bool(getattr(self.state, "desktop", False)),
            "download": self.state.download,
            "record": self.state.record_snapshot(),
            "watch": self.state.watch,
        }

    # ---- GET ----

    def do_GET(self) -> None:  # noqa: N802
        self.state.last_request = time.monotonic()
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                html = files("dly.gui").joinpath(
                    "assets/index.html").read_bytes()
            except (FileNotFoundError, OSError) as exc:
                self._send(500, f"界面资源缺失: {exc}".encode("utf-8"),
                           "text/plain; charset=utf-8")
                return
            # 桌面窗口的 WebView2 没有原生右键菜单，前端据此决定要不要自己画一份。
            # 用注释占位而不是裸变量，静态打开 HTML 时也不会报 ReferenceError。
            flag = (b"<script>window.DLY_DESKTOP=true;</script>"
                    if getattr(self.state, "desktop", False)
                    else b"<script>window.DLY_DESKTOP=false;</script>")
            html = html.replace(b"<!--DLY_DESKTOP_FLAG-->", flag)
            self._send(200, html, "text/html; charset=utf-8")
            return

        if path == "/logo.png":
            # 顶栏图标：assets 目录整体随包分发，这里只开放这一个静态文件
            try:
                png = files("dly.gui").joinpath("assets/logo.png").read_bytes()
            except (FileNotFoundError, OSError):
                self._send(404, b"", "image/png")
                return
            self._send(200, png, "image/png")
            return

        if path in ("/pixel-toggle.js", "/fluid-nav.js"):
            # 主题开关 / 流体导航的独立脚本；assets 整体随包分发，按白名单开放
            try:
                script = files("dly.gui").joinpath("assets" + path).read_bytes()
            except (FileNotFoundError, OSError) as exc:
                self._send(404, f"界面资源缺失: {exc}".encode("utf-8"),
                           "text/plain; charset=utf-8")
                return
            self._send(200, script, "application/javascript; charset=utf-8")
            return

        if path == "/api/clipboard":
            # 桌面窗口没有原生右键菜单，界面自己画的那份要靠这里读系统剪贴板
            from dly.core import clipboard

            text = clipboard.read_text()
            self._json({"ok": text is not None, "text": text or ""})
            return

        if path == "/api/settings":
            self._json({
                "settings": settings_from_config(load_config()),
                "path": str(config_file_path()),
            })
            return

        if path == "/api/state":
            from urllib.parse import parse_qs

            query = parse_qs(urlparse(self.path).query)
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._json({
                "snapshot": self._snapshot(),
                "events": self.state.events_since(after),
            })
            return

        self._json({"error": "not found"}, 404)

    # ---- POST ----

    def do_POST(self) -> None:  # noqa: N802
        self.state.last_request = time.monotonic()
        path = urlparse(self.path).path
        data = self._read_json()

        if path == "/api/settings":
            try:
                target = save_settings(data)
            except (OSError, ValueError) as exc:
                logger.warning("保存设置失败：%s", exc)
                self._json({"error": f"保存设置失败：{exc}"}, 500)
                return
            self._json({
                "ok": True,
                "settings": settings_from_config(load_config()),
                "path": str(target),
            })
            return

        if path == "/api/download":
            text = (data.get("text") or "").strip()
            if not text:
                self._json({"error": "请输入链接"}, 400)
                return
            if self.state.download.get("active"):
                self._json({"error": "已有下载任务在进行"}, 409)
                return
            quality = data.get("quality") or "best"
            outdir = (data.get("output_dir") or "").strip()
            fps = normalize_fps(data.get("fps"))
            threading.Thread(target=_run_download,
                             args=(self.state, text, quality, outdir, fps,
                                   bool(data.get("keep_original"))),
                             daemon=True).start()
            self._json({"ok": True})
            return

        if path == "/api/record/probe":
            url = (data.get("url") or "").strip()
            if len(url) > 500:
                self._json({"ok": False, "error": "链接过长"})
                return
            try:
                self._json(probe_live(url))
            except Exception as exc:  # noqa: BLE001 探测失败也要给前端可读结果
                logger.warning("探测直播间失败：%s", exc)
                self._json({"ok": False, "error": f"识别失败：{exc}"})
            return

        if path == "/api/record/start":
            url = (data.get("url") or "").strip()
            if not url:
                self._json({"error": "请输入直播间链接"}, 400)
                return
            if self.state.record.get("active"):
                self._json({"error": "正在录制中"}, 409)
                return
            segment = parse_segment_seconds(data)
            auto_next = data.get("auto_next") is not False
            outdir = (data.get("output_dir") or "").strip()
            fps = normalize_fps(data.get("fps"))
            self.state.record_stop.clear()
            threading.Thread(target=_run_record,
                             args=(self.state, url, outdir, segment, fps,
                                   auto_next),
                             daemon=True).start()
            self._json({"ok": True})
            return

        if path == "/api/record/stop":
            recorder = self.state.recorder
            if recorder is None:
                self._json({"error": "当前没有在录制的任务"}, 409)
                return
            self.state.log("record", "正在停止录制…")
            self.state.set_record(status="正在停止…", level="idle")
            # 先置位再停 ffmpeg：自动续录的探测间隙里也能立刻收手
            self.state.record_stop.set()
            recorder.stop()
            self._json({"ok": True})
            return

        if path == "/api/watch/start":
            rooms = data.get("rooms") or []
            if isinstance(rooms, str):
                rooms = rooms.splitlines()
            rooms = [r.strip() for r in rooms
                     if r and r.strip() and not r.strip().startswith("#")]
            if not rooms:
                self._json({"error": "请填写至少一个直播间链接"}, 400)
                return
            if self.state.watch.get("active"):
                self._json({"error": "值守已在运行"}, 409)
                return
            try:
                interval = max(10, int(data.get("interval") or 60))
            except (TypeError, ValueError):
                interval = 60
            fps = normalize_fps(data.get("fps"))
            threading.Thread(target=_run_watch,
                             args=(self.state, rooms, interval, fps),
                             daemon=True).start()
            self._json({"ok": True})
            return

        if path == "/api/watch/stop":
            monitor = self.state.monitor
            if monitor is None:
                self._json({"error": "当前没有值守任务"}, 409)
                return
            self.state.log("watch", "正在停止值守…")
            monitor.stop()
            self._json({"ok": True})
            return

        if path == "/api/cookie/check":
            browser = data.get("browser") or "chrome"
            threading.Thread(target=_run_cookie_check,
                             args=(self.state, browser), daemon=True).start()
            self._json({"ok": True})
            return

        if path == "/api/cookie/clear":
            from dly.cookies.manager import CookieManager

            try:
                CookieManager(load_config().cookies).clear_cache()
                self.state.log(self.state.active_tab(),
                               "已清空 Cookie 缓存，下次会重新从浏览器提取", "ok")
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 500)
            return

        if path == "/api/pick-folder":
            # 弹系统「选择文件夹」窗口：同步等待用户选完再返回，前端据此回填输入框
            from dly.core.folder_dialog import pick_folder

            if not _PICK_LOCK.acquire(blocking=False):
                self._json({"error": "已经有一个选择窗口开着"}, 409)
                return
            try:
                picked = pick_folder((data.get("path") or "").strip(),
                                     (data.get("title") or "选择保存目录").strip())
            except Exception as exc:  # noqa: BLE001 弹窗失败不该拖垮服务
                logger.exception("选择目录失败")
                self._json({"error": str(exc)}, 500)
                return
            finally:
                _PICK_LOCK.release()
            self._json({"path": picked, "cancelled": not picked})
            return

        if path == "/api/pick-cookie-file":
            # 导出 cookies.txt 后在这里选回来，路径存设置里，文件本身不入库
            from dly.core.folder_dialog import pick_file

            if not _PICK_LOCK.acquire(blocking=False):
                self._json({"error": "已经有一个选择窗口开着"}, 409)
                return
            try:
                picked = pick_file((data.get("path") or "").strip(),
                                   "选择 cookies.txt")
            except Exception as exc:  # noqa: BLE001 弹窗失败不该拖垮服务
                logger.exception("选择 Cookie 文件失败")
                self._json({"error": str(exc)}, 500)
                return
            finally:
                _PICK_LOCK.release()
            self._json({"path": picked, "cancelled": not picked})
            return

        if path == "/api/open":
            target = (data.get("path") or "").strip()
            try:
                folder = Path(target or load_config().output_dir)
                folder.mkdir(parents=True, exist_ok=True)
                open_folder(str(folder))
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 500)
            return

        if path == "/api/clipboard":
            from dly.core import clipboard

            text = data.get("text")
            ok = isinstance(text, str) and clipboard.write_text(text)
            self._json({"ok": bool(ok)})
            return

        if path == "/api/quit":
            self.state.request_quit()
            self._json({"ok": True})
            return

        self._json({"error": "not found"}, 404)


def _make_server(state: AppState) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state": state})
    last_error: Optional[OSError] = None
    for offset in range(PORT_RANGE):
        port = DEFAULT_PORT + offset
        try:
            return ThreadingHTTPServer((HOST, port), handler)
        except OSError as exc:     # 端口占用，继续试下一个
            last_error = exc
    raise RuntimeError(f"无法启动本地服务：{last_error}")


class AppContext:
    """一次界面运行所需的全部资源：共享状态、HTTP 服务与监听线程。"""

    def __init__(self, state: AppState, server: ThreadingHTTPServer,
                 desktop: bool = False):
        self.state = state
        self.server = server
        self.desktop = desktop
        self.url = f"http://{HOST}:{server.server_address[1]}/"
        self._thread = threading.Thread(target=server.serve_forever,
                                        daemon=True, name="http-server")

    def start(self) -> None:
        self._thread.start()

    def wait(self, interval: float = 0.4) -> None:
        """阻塞到用户请求退出（关窗口或请求 /api/quit）。"""
        while not self.state.quit_requested:
            self._thread.join(timeout=interval)

    def shutdown(self) -> None:
        """停止所有任务并关闭服务；可重复调用。"""
        _stop_tasks(self.state)
        try:
            self.server.shutdown()
        except Exception:  # noqa: BLE001 服务可能已关闭
            pass
        finally:
            # shutdown() 只让 serve_forever 退出，监听 socket 还开着。Windows 上
            # SO_REUSEADDR 允许第二个服务绑同一端口，残留 socket 会把新连接接走
            # 却没人 accept，表现为随机请求超时（反复起停服务的测试里尤其明显）。
            try:
                self.server.server_close()
            except Exception:  # noqa: BLE001 关闭失败也不影响退出
                pass


def _stop_tasks(state: AppState) -> None:
    """先停任务再退进程，避免留下孤儿 ffmpeg 继续写文件。"""
    state.record_stop.set()
    try:
        if state.recorder is not None:
            state.recorder.stop()
        if state.monitor is not None:
            state.monitor.stop()
    except Exception:  # noqa: BLE001
        logger.debug("停止任务时出错", exc_info=True)


def create_app(desktop: bool = False) -> AppContext:
    """创建状态与本地服务（不启动监听，由调用方决定窗口形态）。"""
    # 打包成窗口程序后没有控制台，setup_logging 会自动只写文件
    setup_logging()
    state = AppState()
    state.desktop = desktop
    log = logging.getLogger("dly")
    if not any(isinstance(h, _LogBridge) for h in log.handlers):
        log.addHandler(_LogBridge(state))
    return AppContext(state, _make_server(state), desktop=desktop)


def browser_exe(name: str) -> str:
    """按浏览器进程名找实际 exe 路径；优先注册表 App Paths（可能在 D 盘）。"""
    if sys.platform != "win32" or winreg is None:
        return ""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}",
            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        value, _ = winreg.QueryValueEx(key, None)
        if value and Path(value).is_file():
            return str(value)
    except OSError:
        pass
    roots = [os.environ.get("ProgramFiles(x86)", ""),
             os.environ.get("ProgramFiles", ""),
             r"C:\Program Files (x86)", r"C:\Program Files",
             r"D:\Program Files (x86)", r"D:\Program Files"]
    folder = "Microsoft/Edge/Application" if name == "msedge.exe" else "Google/Chrome/Application"
    for root in roots:
        if not root:
            continue
        candidate = Path(root) / folder / name
        if candidate.is_file():
            return str(candidate)
    return ""


def open_interface(url: str) -> bool:
    """打开界面：Windows 上优先用 Edge / Chrome 应用窗口，失败再退回默认浏览器。

    浏览器模式的 webbrowser.open 在部分环境里返回 True 但看不到窗口；
    ``--app`` 模式会开一个独立应用窗口（没有地址栏标签），视觉上和桌面
    窗口一致，也不需要装 WebView2 / .NET。
    """
    if sys.platform == "win32":
        for name in ("msedge.exe", "chrome.exe"):
            exe = browser_exe(name)
            if not exe:
                continue
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1180,860"],
                                 close_fds=True)
                logger.info("已用 %s 应用窗口打开界面：%s", name, url)
                return True
            except OSError:
                logger.debug("%s 启动失败，继续尝试", name, exc_info=True)
    return bool(webbrowser.open(url))


def _existing_instance_url() -> str:
    """已有 DLY 服务在跑时返回它的地址，避免第二个进程抢端口后闪退。"""
    for port in range(DEFAULT_PORT, DEFAULT_PORT + PORT_RANGE):
        url = f"http://{HOST}:{port}/"
        try:
            with urllib.request.urlopen(url + "api/state?after=0", timeout=0.4) as resp:
                if resp.status == 200:
                    json.loads(resp.read())
                    return url
        except Exception:  # noqa: BLE001 端口没开 / 不是 DLY 都继续扫
            continue
    return ""


def launch(open_browser: bool = True,
           idle_exit_seconds: float = 0.0) -> None:
    """浏览器模式：启动服务并打开系统浏览器（阻塞直到用户退出）。

    ``idle_exit_seconds`` 大于 0 时启用空闲自退：超过该时长没收到任何请求
    且没有任务在跑，就自动退出。给打包版桌面窗口启动失败退回浏览器模式用——
    打包版没有控制台窗口，用户关掉浏览器页面后进程必须能自己收尾。
    """
    # 已经在跑就别再抢端口：直接把已有实例的应用窗口拉出来
    existing = _existing_instance_url()
    if existing:
        logger.info("已有 DLY 在运行，直接打开已有界面：%s", existing)
        if open_browser:
            open_interface(existing)
        return

    app = create_app(desktop=False)
    app.start()
    print(f"界面已启动：{app.url}")
    print("关闭此窗口或按 Ctrl+C 即可退出。")

    if open_browser:
        threading.Timer(0.6, lambda: open_interface(app.url)).start()

    if idle_exit_seconds > 0:
        state = app.state

        def _idle_watcher() -> None:
            """页面关掉后轮询停止；空闲且无任务时替用户把进程退干净。"""
            interval = 10.0
            while not state.quit_requested:
                time.sleep(interval)
                idle = time.monotonic() - state.last_request
                # 页面隐藏时浏览器会把定时器节流到约 1 次/分钟，阈值要留足余量
                if idle >= idle_exit_seconds and not state.busy():
                    state.log("download", "浏览器页面已关闭且无任务在跑，程序自动退出。")
                    state.request_quit()
                    return

        threading.Thread(target=_idle_watcher, daemon=True,
                         name="idle-exit-watcher").start()

    try:
        app.wait()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        print("已退出。")
