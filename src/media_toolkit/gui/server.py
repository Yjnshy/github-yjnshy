"""图形界面：内置 HTTP 服务 + 单页前端。

窗口形态有两种，共用同一套界面与接口：

- **桌面窗口**（默认）：由 :mod:`media_toolkit.gui.desktop` 用 pywebview 承载
- **浏览器**：``mtk gui --web``，调试或没有 WebView2 时使用

为什么界面用网页技术：Tkinter 依赖 tcl/tk，部分精简版 Python 不带（本项目
实测环境就没有），而 Web 界面用标准库 http.server 即可，跨平台一致，
后续做手机端也能直接复用同一套前端。

安全约定：只绑定 127.0.0.1，端口固定范围内递增；不对局域网暴露。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from media_toolkit import __version__
from media_toolkit.core.compat import open_folder
from media_toolkit.core.config import load_config
from media_toolkit.core.ffmpeg import find_ffmpeg
from media_toolkit.core.transcode import FPS_CHOICES, normalize_fps

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_RANGE = 20
MAX_EVENTS = 1200


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
            "seconds": 0.0,
            "size": 0,
        }
        self.recorder = None
        self.monitor = None
        self.desktop = False          # 桌面窗口模式下前端会隐藏「退出程序」提示
        self._quit = threading.Event()

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

    # ---- 退出 ----

    def request_quit(self) -> None:
        self._quit.set()

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
    from media_toolkit.downloader.engine import DownloadEngine

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


def _run_record(state: AppState, url: str, outroot: str, segment: int,
                fps: int = 0) -> None:
    from media_toolkit.cookies.manager import CookieManager
    from media_toolkit.platforms import find_adapter
    from media_toolkit.recorder.recorder import LiveRecorder, RecordingError

    state.set_record(active=True, status="正在获取直播流…", level="idle",
                     seconds=0.0, size=0)
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
            segment_minutes=segment,
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

        path = recorder.record_once(live)
        state.log("record", f"已保存：{path}", "ok")
        state.set_record(status="已停止", level="idle")
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
        state.set_record(active=False)


def _run_watch(state: AppState, rooms: List[str], interval: int,
               fps: int = 0) -> None:
    from media_toolkit.recorder.monitor import LiveMonitor

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
    from media_toolkit.cookies.manager import CookieManager

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
                state.log("download", f"{platform}：未取到（详见上方提示）", "warn")
        manager.cleanup()
    except Exception as exc:  # noqa: BLE001
        state.log("download", f"Cookie 检测异常：{exc}", "error")


# ----------------------------------------------------------------------
# HTTP 层
# ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    state: AppState
    server_version = "MediaToolkit"
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
            "ffmpeg": bool(find_ffmpeg(config.ffmpeg_path)),
            "desktop": bool(getattr(self.state, "desktop", False)),
            "download": self.state.download,
            "record": self.state.record,
            "watch": self.state.watch,
        }

    # ---- GET ----

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                html = files("media_toolkit.gui").joinpath(
                    "assets/index.html").read_bytes()
            except (FileNotFoundError, OSError) as exc:
                self._send(500, f"界面资源缺失: {exc}".encode("utf-8"),
                           "text/plain; charset=utf-8")
                return
            self._send(200, html, "text/html; charset=utf-8")
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
        path = urlparse(self.path).path
        data = self._read_json()

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

        if path == "/api/record/start":
            url = (data.get("url") or "").strip()
            if not url:
                self._json({"error": "请输入直播间链接"}, 400)
                return
            if self.state.record.get("active"):
                self._json({"error": "正在录制中"}, 409)
                return
            try:
                segment = max(0, int(data.get("segment") or 0))
            except (TypeError, ValueError):
                segment = 0
            outdir = (data.get("output_dir") or "").strip()
            fps = normalize_fps(data.get("fps"))
            threading.Thread(target=_run_record,
                             args=(self.state, url, outdir, segment, fps),
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
            from media_toolkit.cookies.manager import CookieManager

            try:
                CookieManager(load_config().cookies).clear_cache()
                self.state.log(self.state.active_tab(),
                               "已清空 Cookie 缓存，下次会重新从浏览器提取", "ok")
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 500)
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
        """阻塞到用户请求退出（关窗口或点「退出程序」）。"""
        while not self.state.quit_requested:
            self._thread.join(timeout=interval)

    def shutdown(self) -> None:
        """停止所有任务并关闭服务；可重复调用。"""
        _stop_tasks(self.state)
        try:
            self.server.shutdown()
        except Exception:  # noqa: BLE001 服务可能已关闭
            pass


def _stop_tasks(state: AppState) -> None:
    """先停任务再退进程，避免留下孤儿 ffmpeg 继续写文件。"""
    try:
        if state.recorder is not None:
            state.recorder.stop()
        if state.monitor is not None:
            state.monitor.stop()
    except Exception:  # noqa: BLE001
        logger.debug("停止任务时出错", exc_info=True)


def create_app(desktop: bool = False) -> AppContext:
    """创建状态与本地服务（不启动监听，由调用方决定窗口形态）。"""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    state = AppState()
    state.desktop = desktop
    log = logging.getLogger("media_toolkit")
    if not any(isinstance(h, _LogBridge) for h in log.handlers):
        log.addHandler(_LogBridge(state))
    return AppContext(state, _make_server(state), desktop=desktop)


def launch(open_browser: bool = True) -> None:
    """浏览器模式：启动服务并打开系统浏览器（阻塞直到用户退出）。"""
    app = create_app(desktop=False)
    app.start()
    print(f"界面已启动：{app.url}")
    print("关闭此窗口或按 Ctrl+C 即可退出。")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(app.url)).start()

    try:
        app.wait()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        print("已退出。")
