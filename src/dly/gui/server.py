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
import mimetypes
import re
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
from dly.core.compat import open_file, open_folder
from dly.core.config import (config_file_path, load_config,
                             save_settings, settings_from_config)
from dly.core import paths
from dly.core.ffmpeg import find_ffmpeg
from dly.core.library import Library, is_audio, is_video
from dly.core.logging import setup_logging
from dly.core.transcode import FPS_CHOICES, normalize_fps
from dly.cookies import sessions

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_RANGE = 20
MAX_HISTORY = 200
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
        self.window_actions: Dict[str, Any] = {}   # 自绘标题栏的窗口操作（桌面模式由 desktop.launch 注入）
        self.new_results = self._load_new_results()  # 上次查看历史后新完成的下载 / 录制数（铃铛红点）
        self.last_request = time.monotonic()   # 最近一次收到 HTTP 请求的时刻（浏览器模式空闲自退用）
        self._quit = threading.Event()
        # 独立于 recorder 内部状态的停止信号：自动续录的探测间隙里也要能停下
        self.record_stop = threading.Event()
        # 任务历史：界面把它画成表格，点一行就能跳到该文件所在目录
        self.history: List[Dict[str, Any]] = self._load_history()
        self._history_seq = max((int(r.get("id") or 0)
                                 for r in self.history), default=0)
        # 本地歌单：下载/提取出来的音频在这里排队，界面上的「歌单」页读它
        self.library = Library()
        # 抖音「喜欢/收藏」批量导入（独立状态机，耗时操作在后台线程）
        from dly.gui.dyimport_state import DyImportState
        self.dyimport = DyImportState(lambda tab, msg, lvl="info": self.log(tab, msg, lvl))
        # 汽水音乐分享链接采集（免登录，音频与封面直接进歌单）
        from dly.gui.qishui_state import QishuiState
        self.qishui = QishuiState(
            lambda tab, msg, lvl="info": self.log(tab, msg, lvl), self.library)

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

    @staticmethod
    def _history_file() -> Path:
        return paths.data_dir() / "history.json"

    @classmethod
    def _load_history(cls) -> List[Dict[str, Any]]:
        """从磁盘读回历史记录（换一次进程也还在）。"""
        try:
            data = json.loads(cls._history_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        rows = []
        for item in data[-MAX_HISTORY:]:
            if not isinstance(item, dict) or not item.get("kind"):
                continue
            rows.append({
                "id": int(item.get("id") or 0),
                "kind": str(item.get("kind")),
                "name": str(item.get("name") or ""),
                "path": str(item.get("path") or ""),
                "size": int(item.get("size") or 0),
                "status": str(item.get("status") or "完成"),
                "at": float(item.get("at") or 0),
            })
        return rows

    @staticmethod
    def _unread_file():
        from dly.core import paths
        return paths.data_dir() / "unread.json"

    @classmethod
    def _load_new_results(cls) -> int:
        try:
            import json
            data = json.loads(cls._unread_file().read_text(encoding="utf-8"))
            return max(0, min(99, int(data.get("new_results") or 0)))
        except Exception:  # noqa: BLE001 文件不存在 / 坏了就当 0
            return 0

    def _save_new_results(self) -> None:
        try:
            import json
            self._unread_file().write_text(
                json.dumps({"new_results": self.new_results}, ensure_ascii=False),
                encoding="utf-8")
        except Exception:  # noqa: BLE001 写不了不影响主流程
            pass

    def _save_history(self) -> None:
        try:
            path = self._history_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.history, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.debug("保存历史记录失败: %s", exc)

    def add_history(self, kind: str, name: str, path: str = "",
                    size: int = 0, status: str = "完成") -> int:
        """记一条任务（下载 / 录制），返回条目 id。

        任务开始时就先建一条「录制中 / 下载中」，边跑边更新大小，
        结束时再补上文件路径——否则长时间录制时表格一直是空的。
        """
        with self._lock:
            self._history_seq += 1
            self.history.append({
                "id": self._history_seq,
                "kind": kind,
                "name": str(name),
                "path": str(path),
                "size": int(size or 0),
                "status": status,
                "at": time.time(),
            })
            if len(self.history) > MAX_HISTORY:
                del self.history[: len(self.history) - MAX_HISTORY]
            if status in ("完成", "已停止"):
                self.new_results = min(99, self.new_results + 1)
                self._save_new_results()
            self._save_history()
            return self._history_seq

    def update_history(self, entry_id: int, **fields: Any) -> None:
        """更新某条任务记录（大小、状态、最终路径）。"""
        if not entry_id:
            return
        with self._lock:
            for row in self.history:
                if row["id"] == entry_id:
                    if "size" in fields:
                        fields["size"] = int(fields["size"] or 0)
                    row.update(fields)
                    # 进度刷新（只有 size 在变）不必每次落盘；落终态时再写
                    if ("path" in fields or "status" in fields) and \
                            row.get("status") not in ("录制中", "下载中"):
                        if fields.get("status") in ("完成", "已停止"):
                            self.new_results = min(99, self.new_results + 1)
                            self._save_new_results()
                        self._save_history()
                    return

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
                  fps: int = 0, keep_original: bool = False,
                  add_to_library: bool = False,
                  extract_mp3: bool = False) -> None:
    from dly.downloader.engine import DownloadEngine

    with state._lock:  # noqa: SLF001 内部状态，避免并发重复启动
        state.download["active"] = True
        state.download["progress"] = {"percent": 0, "text": "准备中…"}
    state.log("download", "开始解析链接…")
    if fps:
        state.log("download", f"下载完成后转码到 {fps}fps（需重新编码，比较耗时）")

    # yt-dlp 的进度回调非常密集，界面上按 1.5 秒节流，否则日志会被刷屏
    last = {"at": 0.0}
    permission_denied = {"hit": False}
    pending: List[Dict[str, Any]] = []      # 本次任务里「下载中」的表格条目

    def progress(message: str) -> None:
        # 引擎每开始处理一条链接都会发 "[1/3] https://..."，借它把行先占上
        started = re.match(r"^\[(\d+)/(\d+)\]\s+(\S+)", message or "")
        if started:
            entry = state.add_history("download", started.group(3)[:80],
                                      "", 0, "下载中")
            pending.append({"id": entry})
        if "Permission denied" in message or "拒绝访问" in message:
            permission_denied["hit"] = True
        if "下载中" in message or "%" in message:
            now = time.monotonic()
            if now - last["at"] < 1.5:
                return
            last["at"] = now
        # 把 yt-dlp 的百分比进度转成结构化字段，前端显示进度条
        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", message or "")
        if pct and state.download.get("active"):
            state.download["progress"] = {
                "percent": min(100.0, float(pct.group(1))),
                "text": (message or "").strip()[-120:],
            }
        state.log("download", message)

    try:
        config = load_config()
        config.quality = quality
        config.keep_original = bool(keep_original)
        engine = DownloadEngine(config, progress=progress)
        ok, total = engine.download_text(text, quality=quality,
                                         output_dir=outdir, fps=fps)
        outputs = list(engine.last_outputs)
        if outputs:
            state.download["last_file"] = str(outputs[-1])
            state.download["folder"] = str(outputs[-1].parent)
            state.download["outputs"] = [str(p) for p in outputs]
        for index, entry in enumerate(pending):
            if index < len(outputs):
                item = outputs[index]
                state.update_history(
                    entry["id"], path=str(item), name=Path(item).stem,
                    size=Path(item).stat().st_size if Path(item).exists() else 0,
                    status="完成")
            else:
                state.update_history(entry["id"], status="失败")
        # 一条链接产出多个文件（如图文）时，多出来的单独补行
        for item in outputs[len(pending):]:
            state.add_history("download", Path(item).stem, str(item),
                              Path(item).stat().st_size
                              if Path(item).exists() else 0, "完成")
        # 歌单：音频下载直接收进去；视频按用户勾选顺手抽一条 mp3
        for item in outputs:
            try:
                if is_audio(item):
                    if add_to_library:
                        state.library.add(item, source="download")
                        state.log("download", f"已加入歌单：{Path(item).name}")
                elif extract_mp3:
                    track = state.library.add_extracted(item)
                    state.log("download",
                              f"已提取音频并加入歌单：{Path(track['path']).name}")
            except Exception as exc:  # noqa: BLE001 提取失败不该毁掉下载结果
                logger.exception("加入歌单失败")
                state.log("download", f"加入歌单失败：{exc}", "warn")
        level = "ok" if ok == total and total else "warn"
        state.log("download", f"完成：成功 {ok} / 共 {total}", level)
        if permission_denied["hit"] and ok == 0:
            # 原始报错是一串英文 Errno，普通用户看不懂，这里直说怎么办
            state.log("download",
                      "保存失败的原因是没有写入权限，不是链接的问题。"
                      f"请换一个保存目录（现在是 {outdir}），"
                      "例如 D:\\视频；如果安全软件弹过提示，选「允许」。", "error")
    except Exception as exc:  # noqa: BLE001
        logger.exception("下载失败")
        state.log("download", f"下载失败：{exc}", "error")
    finally:
        state.download["active"] = False
        state.download["progress"] = None


def _live_url_hint(url: str) -> str:
    """直播间链接的常见错误提前拦掉，别让用户看到「风控」这种误导信息。"""
    from dly.platforms.bilibili_live import room_id_from_url

    if "live.bilibili.com" in url and not room_id_from_url(url):
        return ("这是 B 站直播首页，不是某个直播间。请打开具体直播间页面，"
                "地址形如 https://live.bilibili.com/12345，再复制过来。")
    return ""


def probe_live(url: str) -> Dict[str, Any]:
    """查询直播间标题 / 主播 / 开播状态，供界面自动回填核对。"""
    from dly.cookies.manager import CookieManager
    from dly.platforms import find_adapter

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"ok": False, "error": "链接格式不对，请以 http(s):// 开头"}
    hint = _live_url_hint(url)
    if hint:
        return {"ok": False, "error": hint}
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

    hint = _live_url_hint(url)
    if hint:
        state.log("record", hint, "error")
        state.set_record(status="链接不是直播间", level="error")
        return

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
        # 当前这段在表格里的条目 id：开始时插入「录制中」，结束时补路径与状态
        current = {"id": 0}

        def _on_progress(seconds: float, size: int) -> None:
            state.set_record(seconds=seconds, size=size)
            state.update_history(current["id"], size=size)

        recorder = LiveRecorder(
            ffmpeg, outdir,
            fmt=config.record.format,
            # 界面填的时长是「录多久就停」，交给 -t 让 ffmpeg 到点自行退出。
            # 不传 segment_seconds：那是无限切文件的 muxer，永远不会自己退出。
            max_duration=segment,
            fps=fps,
            on_progress=_on_progress,
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
            label = live.title or live.anchor or "直播"
            if part > 1:
                label += f"（第 {part} 段）"
            current["id"] = state.add_history("record", label, "", 0, "录制中")
            try:
                path = recorder.record_once(live, part=part if part > 1 else 0)
                state.set_record(ended_at=time.monotonic(),
                                 last_file=str(path), folder=str(Path(path).parent))
                state.log("record", f"已保存：{path}", "ok")
                state.update_history(
                    current["id"], path=str(path),
                    size=Path(path).stat().st_size if Path(path).exists() else 0,
                    status="已停止" if state.record_stop.is_set() else "完成")
                failures = 0
            except RecordingError as exc:
                state.set_record(ended_at=time.monotonic())
                state.update_history(current["id"], status="失败")
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


_LOGIN_LOCK = threading.Lock()
_LOGIN_PROCS: Dict[str, "subprocess.Popen"] = {}


def _spawn_login_window(platform: str, force: bool = False) -> bool:
    """拉起一个独立的登录窗进程，返回是否真的新开了一个。

    ``webview.start()`` 必须独占主线程，而主窗口已经占着它，所以让程序再起
    一个自己：打包版是 ``DLY.exe --login <平台>``，源码模式是
    ``python -m dly.gui --login <平台>``。登录结果写在磁盘上，界面轮询即可。

    同一平台已经有窗口在开时直接忽略：界面按钮被连点两次会发两次请求，
    之前就出现过一次点出两个登录窗。
    """
    global _LOGIN_PROCS
    with _LOGIN_LOCK:
        running = _LOGIN_PROCS.get(platform)
        if running is not None and running.poll() is None and not force:
            logger.info("%s 的登录窗已经开着，忽略重复请求", platform)
            return False

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--login", platform]
            env = None
        else:
            cmd = [sys.executable, "-m", "dly.gui", "--login", platform]
            env = dict(os.environ)
            src = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (src, env.get("PYTHONPATH", "")) if part)

        flags = 0
        if os.name == "nt":
            flags = 0x00000008        # DETACHED_PROCESS：不占外壳、不挂控制台
        proc = subprocess.Popen(cmd, env=env, creationflags=flags,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        _LOGIN_PROCS[platform] = proc
        return True


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

    # ---- 媒体文件 ----

    def _track_id(self) -> int:
        from urllib.parse import parse_qs

        query = parse_qs(urlparse(self.path).query)
        try:
            return int(query.get("id", ["0"])[0] or 0)
        except ValueError:
            return 0

    def _send_cover(self) -> None:
        """封面图：按曲目 id 取，只允许读歌单里登记过的那张。"""
        path = self.state.library.cover_of(self._track_id())
        if not path:
            self._send(404, b"", "image/jpeg")
            return
        try:
            data = path.read_bytes()
        except OSError:
            self._send(404, b"", "image/jpeg")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _send_lrc(self) -> None:
        """歌词：与音频同目录的同名 .lrc，只返回歌单里登记过的曲目。"""
        path = self.state.library.lrc_of(self._track_id())
        if not path:
            self._send(404, b"", "text/plain; charset=utf-8")
            return
        try:
            data = path.read_bytes()
        except OSError:
            self._send(404, b"", "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_frame(self) -> None:
        """候选封面帧：只允许读歌单缓存目录里的 frame_<id>_<i>.jpg。"""
        from urllib.parse import parse_qs

        query = parse_qs(urlparse(self.path).query)
        try:
            track_id = int(query.get("id", ["0"])[0] or 0)
            index = int(query.get("i", ["0"])[0] or 0)
        except ValueError:
            self._send(404, b"", "image/jpeg")
            return
        path = self.state.library.root / "frames" / f"{track_id}_{index}.jpg"
        if not path.is_file():
            self._send(404, b"", "image/jpeg")
            return
        try:
            data = path.read_bytes()
        except OSError:
            self._send(404, b"", "image/jpeg")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_range(self, path: Path, default_ctype: str, label: str) -> None:
        """音频/视频共用：必须支持 Range。

        浏览器播本地媒体时先发 ``Range: bytes=0-``，拖动进度条还要再发
        区间请求；不支持 206 的话时间轴会跳回开头、时长显示成 Infinity。
        """
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        raw = (self.headers.get("Range") or "").strip()
        match = re.match(r"bytes=(\d*)-(\d*)$", raw)
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
            else:                       # bytes=-500 → 最后 500 字节
                start = max(0, size - int(match.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206

        length = end - start + 1
        ctype = mimetypes.guess_type(path.name)[0] or default_ctype
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as fh:
            fh.seek(start)
            left = length
            try:
                while left > 0:
                    chunk = fh.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # 用户拖进度条/切歌时浏览器会直接断开，属于正常现象，别刷异常
                logger.debug("%s连接被中断：%s", label, path.name)

    def _send_audio(self) -> None:
        track = self.state.library.get(self._track_id())
        if not track:
            self._send(404, b"", "audio/mpeg")
            return
        path = Path(track["path"])
        if not path.is_file():
            self._send(404, b"", "audio/mpeg")
            return
        self._send_range(path, "audio/mpeg", "音频")

    def _send_video(self) -> None:
        path = self.state.library.video_of(self._track_id())
        if not path:
            self._send(404, b"", "video/mp4")
            return
        self._send_range(path, "video/mp4", "视频")

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
            "logins": sessions.status(),
            "history": list(self.state.history[-MAX_HISTORY:]),
            "new_results": self.state.new_results,
            "history_path": str(AppState._history_file()),
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

        allowed_script = (
            path in ("/pixel-toggle.js", "/fluid-nav.js", "/music-tunnel.js",
                     "/three/three.module.min.js", "/three/three.core.min.js")
            or (path.startswith("/three/addons/") and ".." not in path
                and path.endswith(".js"))
        )
        if allowed_script:
            # 主题开关 / 流体导航 / 歌单隧道（含内置 three.js 与其后期处理 addon）；
            # assets 整体随包分发，按白名单开放，避免变成任意文件服务器
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

        if path == "/api/login/sites":
            self._json({"sites": sessions.status()})
            return

        if path == "/api/dyimport/status":
            self._json(self.state.dyimport.status())
            return

        if path == "/api/qishui/status":
            self._json(self.state.qishui.status())
            return

        if path == "/api/library":
            library = self.state.library
            self._json({
                "tracks": library.public(),
                "folder": str(library.root),
                "ffmpeg": bool(find_ffmpeg(load_config().ffmpeg_path)),
            })
            return

        if path == "/api/media/audio":
            self._send_audio()
            return

        if path == "/api/media/video":
            self._send_video()
            return

        if path == "/api/media/cover":
            self._send_cover()
            return

        if path == "/api/media/frame":
            self._send_frame()
            return

        if path == "/api/media/lrc":
            self._send_lrc()
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
                                   bool(data.get("keep_original")),
                                   bool(data.get("add_to_library")),
                                   bool(data.get("extract_mp3"))),
                             daemon=True).start()
            self._json({"ok": True})
            return

        # 抖音「喜欢 / 收藏」批量导入 -------------------------------------
        if path == "/api/dyimport/login":
            try:
                self.state.dyimport.start_login()
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 409)
            return

        if path == "/api/dyimport/check":
            self.state.dyimport.check_login()
            self._json({"ok": True})
            return

        if path == "/api/dyimport/scan":
            tabs = data.get("tabs") or []
            try:
                self.state.dyimport.start_scan(tabs if isinstance(tabs, list) else [tabs])
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 409)
            return

        if path == "/api/dyimport/stop":
            self.state.dyimport.stop_scan()
            self._json({"ok": True})
            return

        if path == "/api/dyimport/download":
            ids = data.get("ids") or []
            if not isinstance(ids, list) or not ids:
                self._json({"error": "请先勾选要下载的视频"}, 400)
                return
            if self.state.download.get("active"):
                self._json({"error": "已有下载任务在进行"}, 409)
                return
            from dly.core.douyin_import import video_url
            # 走现有下载管线：拼成多行链接，复用画质 / 目录 / 转码等全部设置
            text = "\n".join(video_url(str(i)) for i in ids)
            quality = data.get("quality") or "best"
            outdir = (data.get("output_dir") or "").strip()
            fps = normalize_fps(data.get("fps"))
            threading.Thread(target=_run_download,
                             args=(self.state, text, quality, outdir, fps,
                                   bool(data.get("keep_original")),
                                   bool(data.get("add_to_library")),
                                   bool(data.get("extract_mp3"))),
                             daemon=True).start()
            self._json({"ok": True, "count": len(ids)})
            return

        # 汽水音乐分享链接采集 ---------------------------------------------
        if path == "/api/qishui/start":
            text = (data.get("text") or "").strip()
            if not text:
                self._json({"error": "先粘贴汽水音乐的分享链接"}, 400)
                return
            try:
                self.state.qishui.start(
                    text,
                    output_dir=(data.get("output_dir") or "").strip(),
                    limit=int(data.get("limit") or 0))
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 409)
            return

        if path == "/api/qishui/stop":
            self.state.qishui.stop()
            self._json({"ok": True})
            return

        if path == "/api/qishui/login":
            try:
                self.state.qishui.start_login()
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 409)
            return

        if path == "/api/qishui/login/cancel":
            self.state.qishui.cancel_login()
            self._json({"ok": True})
            return

        if path == "/api/qishui/logout":
            self.state.qishui.logout()
            self._json({"ok": True})
            return

        if path == "/api/qishui/cookie":
            try:
                self.state.qishui.login_with_cookie(
                    (data.get("cookie") or "").strip())
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
            return

        if path == "/api/qishui/playlists":
            try:
                self._json({"ok": True,
                            "playlists": self.state.qishui.load_playlists()})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 409)
            return

        if path == "/api/qishui/playlist/start":
            pid = (data.get("playlist_id") or "").strip()
            if not pid:
                self._json({"error": "先选一个歌单"}, 400)
                return
            try:
                self.state.qishui.start_from_playlist(
                    pid, limit=int(data.get("limit") or 0))
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 409)
            return

        if path == "/api/library/add":
            target = (data.get("path") or "").strip()
            try:
                track = self.state.library.add(target, source="import")
            except FileNotFoundError as exc:
                self._json({"error": str(exc)}, 400)
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("加入歌单失败")
                self._json({"error": f"加入歌单失败：{exc}"}, 500)
                return
            self._json({"ok": True, "track": track, "tracks": self.state.library.public()})
            return

        if path == "/api/library/extract":
            # 支持单个 path 或批量 paths 数组（「从视频提取」多选）
            raw = data.get("paths") if isinstance(data.get("paths"), list) \
                else ([data.get("path") or ""] if data.get("path") else [])
            targets = [(str(p) or "").strip() for p in raw]
            targets = [p for p in targets if p]
            if not targets:
                self._json({"error": "先选一个视频文件"}, 400)
                return
            added: List[dict] = []
            failed: List[dict] = []
            for target in targets:
                if not is_video(target) and not is_audio(target):
                    failed.append({"path": target, "error": "只支持视频或音频文件"})
                    continue
                try:
                    if is_audio(target):
                        track = self.state.library.add(target, source="import")
                    else:
                        track = self.state.library.add_extracted(target)
                    added.append(track)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("提取音频失败")
                    failed.append({"path": target, "error": str(exc)})
            self._json({"ok": True, "tracks": added, "failed": failed,
                        "tracks_all": self.state.library.public()})
            return

        if path == "/api/library/frames":
            try:
                track_id = int(data.get("id") or 0)
            except (TypeError, ValueError):
                track_id = 0
            frames = self.state.library.frames(track_id, int(data.get("count") or 8)
                                               if str(data.get("count") or "").isdigit()
                                               else 8)
            self._json({
                "ok": True,
                "frames": [{"i": f["i"], "at": f["at"],
                            "url": f"/api/media/frame?id={track_id}&i={f['i']}"}
                           for f in frames],
            })
            return

        if path == "/api/library/cover":
            try:
                track_id = int(data.get("id") or 0)
                index = int(data.get("i") or 0)
            except (TypeError, ValueError):
                self._json({"error": "参数不对"}, 400)
                return
            if not self.state.library.use_frame(track_id, index):
                self._json({"error": "这一帧不可用，重新挑一张试试"}, 400)
                return
            self._json({"ok": True, "tracks": self.state.library.public()})
            return

        if path == "/api/library/update":
            try:
                track_id = int(data.get("id") or 0)
            except (TypeError, ValueError):
                track_id = 0
            fields = {k: v for k, v in data.items() if k in ("title", "last_pos")}
            if not self.state.library.update(track_id, **fields):
                self._json({"error": "曲目不存在"}, 404)
                return
            self._json({"ok": True})
            return

        if path == "/api/library/remove":
            try:
                track_id = int(data.get("id") or 0)
            except (TypeError, ValueError):
                track_id = 0
            ok = self.state.library.remove(track_id,
                                           bool(data.get("delete_file")))
            self._json({"ok": ok, "tracks": self.state.library.public()})
            return

        if path == "/api/pick-media":
            # 选音频/视频文件：歌单导入和「从视频提取」都用它。
            # multiple=1 时走多选对话框，返回 paths 数组（「从视频提取」批量）。
            from dly.core.folder_dialog import pick_file, pick_media_files

            if not _PICK_LOCK.acquire(blocking=False):
                self._json({"error": "已经有一个选择窗口开着"}, 409)
                return
            multiple = bool(data.get("multiple"))
            try:
                if multiple:
                    picked = pick_media_files(
                        (data.get("path") or "").strip(),
                        data.get("title") or "选择视频或音频（可多选）")
                    self._json({"paths": picked,
                                "cancelled": not picked})
                else:
                    picked = pick_file(
                        (data.get("path") or "").strip(),
                        data.get("title") or "选择音频或视频")
                    self._json({"path": picked, "cancelled": not picked})
            except Exception as exc:  # noqa: BLE001
                logger.exception("选择媒体文件失败")
                self._json({"error": str(exc)}, 500)
            finally:
                _PICK_LOCK.release()
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

        if path == "/api/login/start":
            from dly.gui import login_window

            platform = (data.get("platform") or "").strip().lower()
            if login_window.site_of(platform) is None:
                self._json({"error": "不支持的登录目标"}, 400)
                return
            try:
                opened = _spawn_login_window(platform)
            except Exception as exc:  # noqa: BLE001 拉不起子进程时如实回报
                logger.exception("打开登录窗口失败")
                self._json({"error": f"登录窗口启动失败：{exc}"}, 500)
                return
            if opened:
                self.state.log(
                    self.state.active_tab(),
                    f"已打开{sessions.site_name(platform)}登录窗口，"
                    "在新窗口里扫码或输入账号即可", "info")
            self._json({"ok": True, "opened": opened})
            return

        if path == "/api/login/logout":
            from dly.cookies import sessions

            platform = (data.get("platform") or "").strip().lower()
            if sessions.clear(platform):
                self.state.log(self.state.active_tab(),
                               f"已退出{sessions.site_name(platform)}登录", "ok")
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "本来就没有登录"})
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

        if path == "/api/files/tree":
            root = (data.get("root") or "").strip()
            if not root:
                root = load_config().output_dir or str(paths.data_dir())
            try:
                base = Path(root).expanduser()
                if not base.is_dir():
                    self._json({"error": "目录不存在或不是文件夹"}, 400)
                    return
                nodes: List[Dict[str, Any]] = []
                for item in sorted(base.iterdir(),
                                   key=lambda p: (p.is_file(), p.name.lower())):
                    if item.name.startswith(".") or item.name in ("$RECYCLE.BIN",
                                                                   "System Volume Information"):
                        continue
                    try:
                        st = item.stat()
                    except OSError:
                        continue
                    node = {"name": item.name, "path": str(item),
                            "type": "dir" if item.is_dir() else "file",
                            "size": st.st_size if item.is_file() else 0,
                            "mtime": st.st_mtime}
                    nodes.append(node)
                    if len(nodes) >= 250:
                        break
                # 文件夹在前（排序已保证），数量截断时提示还有更多
                self._json({"ok": True, "root": str(base), "nodes": nodes,
                            "truncated": len(nodes) >= 250})
            except OSError as exc:
                self._json({"error": f"读取目录失败：{exc}"}, 500)
            return

        if path == "/api/file/open":
            target = (data.get("path") or "").strip()
            if not target or not Path(target).is_file():
                self._json({"error": "文件不存在"}, 400)
                return
            try:
                open_file(target)
                self._json({"ok": True})
            except OSError as exc:
                self._json({"error": str(exc)}, 500)
            return

        if path == "/api/files/search":
            root = (data.get("root") or "").strip()
            keyword = (data.get("keyword") or "").strip().lower()
            if not root:
                root = load_config().output_dir or str(paths.data_dir())
            try:
                base = Path(root).expanduser()
                if not base.is_dir():
                    self._json({"error": "目录不存在或不是文件夹"}, 400)
                    return
                found: List[Dict[str, Any]] = []
                for dirpath, dirnames, filenames in os.walk(base):
                    dirnames[:] = [d for d in dirnames
                                   if not d.startswith(".") and d not in ("$RECYCLE.BIN", "System Volume Information")]
                    for name in filenames:
                        if keyword and keyword not in name.lower():
                            continue
                        item = Path(dirpath) / name
                        try:
                            stat = item.stat()
                        except OSError:
                            continue
                        found.append({"name": name, "path": str(item),
                                      "size": stat.st_size, "mtime": stat.st_mtime})
                        if len(found) >= 300:
                            break
                    if len(found) >= 300:
                        break
                found.sort(key=lambda x: x["mtime"], reverse=True)
                self._json({"ok": True, "root": str(base), "files": found,
                            "truncated": len(found) >= 300})
            except OSError as exc:
                self._json({"error": f"搜索失败：{exc}"}, 500)
            return

        if path == "/api/window":
            action = (data.get("action") or "").strip()
            handler = self.state.window_actions.get(action)
            if not handler:
                self._json({"error": "浏览器模式下不可用"}, 400)
                return
            try:
                handler()
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 500)
            return

        if path == "/api/history/seen":
            with self.state._lock:
                self.state.new_results = 0
                self.state._save_new_results()
            self._json({"ok": True})
            return

        if path == "/api/history/clear":
            with self.state._lock:
                self.state.history.clear()
            self.state._save_history()
            self._json({"ok": True})
            return

        if path == "/api/open":
            target = (data.get("path") or "").strip()
            try:
                folder = Path(target or load_config().output_dir)
                if not folder.is_file():
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


def _ensure_minimize_style_loop(state: "AppState") -> None:
    """无边框窗口缺 WS_MINIMIZEBOX 时任务栏点不动；常驻补样式位。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        wanted = 0x20000 | 0x10000
        while not state.quit_requested:
            time.sleep(1)
            if not state.desktop:
                continue
            found = []

            def cb(h, _):
                n = user32.GetWindowTextLengthW(h)
                if n and user32.IsWindowVisible(h):
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(h, buf, n + 1)
                    if "帅羊羊DLY" in buf.value:
                        found.append(h)
                return True

            try:
                user32.EnumWindows(ENUM(cb), 0)
                for h in found:
                    style = user32.GetWindowLongPtrW(h, -16)
                    if (style & wanted) != wanted:
                        user32.SetWindowLongPtrW(h, -16, style | wanted)
                        user32.SetWindowPos(h, 0, 0, 0, 0, 0,
                                            0x0001 | 0x0002 | 0x0040)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return


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
        if self.state.desktop:
            threading.Thread(target=_ensure_minimize_style_loop,
                             args=(self.state,), daemon=True,
                             name="minimize-style").start()

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
             os.environ.get("ProgramFiles", "")]
    # 枚举所有盘符的 Program Files，浏览器可能装在不常见的盘（不写死 C/D）
    import string as _string
    for letter in _string.ascii_uppercase:
        drive = f"{letter}:\\"
        roots += [drive + "Program Files (x86)", drive + "Program Files"]
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
        # Chrome 的 --app 窗口最可靠；Edge 在部分机器上只弹空白窗口，放兜底
        for name in ("chrome.exe", "msedge.exe"):
            exe = browser_exe(name)
            if not exe:
                continue
            params = f'--app="{url}" --window-size=1180,860'
            old_path = os.environ.get("PATH")
            if getattr(sys, "frozen", False) and old_path:
                # 冻结程序给子进程传的 PATH 含有 _internal，浏览器会加载到
                # 不匹配的 api-ms-win-*.dll 后静默退出；调用前后临时替换。
                os.environ["PATH"] = os.pathsep.join(
                    p for p in old_path.split(os.pathsep)
                    if "_internal" not in p.lower() and "_mei" not in p.lower())
            try:
                # 冻结程序里 subprocess.Popen 会被 PyInstaller 改写，浏览器
                # 子进程会静默消失；系统 ShellExecute 最可靠。
                import ctypes
                rc = ctypes.windll.shell32.ShellExecuteW(
                    None, "open", exe, params, None, 1)
                if int(rc) > 32:
                    logger.info("已用 %s 应用窗口打开界面：%s", name, url)
                    return True
                logger.warning("ShellExecute 启动 %s 失败：%s", name, rc)
            except Exception:
                logger.debug("%s 启动失败，继续尝试", name, exc_info=True)
            finally:
                if old_path is not None:
                    os.environ["PATH"] = old_path
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
