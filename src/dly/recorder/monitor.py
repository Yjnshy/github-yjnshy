"""直播状态监听器：轮询直播间，开播即录、下播即停、断线重连。"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from dly.core.config import AppConfig
from dly.core.ffmpeg import find_ffmpeg
from dly.cookies.manager import CookieManager
from dly.platforms import find_adapter
from dly.recorder.recorder import LiveRecorder, RecordingError

logger = logging.getLogger(__name__)

NotifyCallback = Callable[[str], None]

# 重连退避：首次 10 秒，逐次翻倍，上限 5 分钟
_BACKOFF_BASE = 10
_BACKOFF_CAP = 300


class LiveMonitor:
    """多直播间无人值守监控。

    每个直播间一个监控线程；检测到开播后拉起录制线程，
    录制线程内部处理断线重连（同一场最多重连 max_retries 次）。
    """

    def __init__(self, config: AppConfig,
                 notify: Optional[NotifyCallback] = None):
        self.config = config
        self.cookies = CookieManager(config.cookies)
        self.notify = notify or (lambda msg: logger.info(msg))
        self._stop = threading.Event()
        self._recording: Dict[str, threading.Thread] = {}
        self._recorders: Dict[str, LiveRecorder] = {}
        self._lock = threading.Lock()

        self.ffmpeg = find_ffmpeg(config.ffmpeg_path)
        if not self.ffmpeg:
            raise RuntimeError(
                "未找到 ffmpeg：请安装 ffmpeg 并加入 PATH，"
                "或在 config.yaml 中配置 ffmpeg_path")

    # ------------------------------------------------------------------

    def watch(self, rooms: List[str]) -> None:
        """启动对一组直播间的值守（阻塞直到 stop()）。"""
        if not rooms:
            self.notify("直播间列表为空，请在 config.yaml 的 record.rooms 中添加")
            return
        watchers = []
        for room in rooms:
            t = threading.Thread(target=self._watch_room, args=(room,),
                                 daemon=True, name=f"watch-{room[-12:]}")
            t.start()
            watchers.append(t)
        self.notify(f"已开始值守 {len(rooms)} 个直播间，"
                    f"轮询间隔 {self.config.record.poll_interval} 秒")
        try:
            while not self._stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()
        finally:
            # 等录制线程收尾，避免退出后留下孤儿 ffmpeg 进程占着文件
            for t in watchers:
                t.join(timeout=3)
            with self._lock:
                active = list(self._recording.values())
            for t in active:
                t.join(timeout=5)

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self.notify("正在停止所有录制…")
        with self._lock:
            recorders = list(self._recorders.values())
        for recorder in recorders:
            recorder.stop()

    # ------------------------------------------------------------------

    def _watch_room(self, room_url: str) -> None:
        """单个直播间的轮询循环。"""
        was_live = False
        while not self._stop.is_set():
            live = self._probe(room_url)
            if live and live.is_live and not was_live:
                was_live = True
                self.notify(f"🔴 开播：{live.anchor or room_url}"
                            f"（{live.title}）")
                self._start_recording(live)
            elif live and not live.is_live and was_live:
                was_live = False
                self.notify(f"⚪ 下播：{live.anchor or room_url}")
            self._stop.wait(self.config.record.poll_interval)

    def _start_recording(self, live) -> None:
        with self._lock:
            if live.room_url in self._recording:
                return
            t = threading.Thread(target=self._record_session, args=(live,),
                                 daemon=True, name=f"rec-{live.anchor[:8]}")
            self._recording[live.room_url] = t
            t.start()

    def _record_session(self, live) -> None:
        """一场直播的录制循环：断流后只要还开播就重连。"""
        recorder = LiveRecorder(
            self.ffmpeg,
            Path(self.config.output_dir) / "直播录制",
            fmt=self.config.record.format,
            segment_minutes=self.config.record.segment_minutes,
            on_progress=self._progress_reporter(live),
        )
        with self._lock:
            self._recorders[live.room_url] = recorder
        retries = 0
        part = 0
        try:
            while not self._stop.is_set() and retries <= self.config.record.max_retries:
                part += 1
                try:
                    path = recorder.record_once(live, part=part if part > 1 else 0)
                    self.notify(f"💾 已保存：{path.name}")
                except RecordingError as exc:
                    retries += 1
                    detail = f"（{exc.detail.splitlines()[-1][:120]}）" if exc.detail else ""
                    self.notify(f"⚠️ 录制失败：{exc}{detail}")
                    if not self._retry_later(live, retries):
                        break
                    live = self._fresh_live(live) or live
                    continue

                if self._stop.is_set():
                    break

                # ffmpeg 退出：判断是断流还是真的下播
                fresh = self._fresh_live(live)
                if fresh and fresh.is_live and fresh.stream_url:
                    retries += 1
                    if not self._retry_later(live, retries, reason="断流"):
                        break
                    live = fresh
                    continue
                self.notify(f"⚪ 录制完成：{live.anchor or live.room_url}（共 {part} 段）")
                break
        finally:
            recorder.stop()
            with self._lock:
                self._recorders.pop(live.room_url, None)
                self._recording.pop(live.room_url, None)

    def _retry_later(self, live, retries: int, reason: str = "失败") -> bool:
        """退避等待；返回 False 表示应放弃重连。"""
        if retries > self.config.record.max_retries:
            self.notify(f"⛔ {reason}重试已达上限（{self.config.record.max_retries} 次），"
                        f"停止录制：{live.anchor or live.room_url}")
            return False
        delay = min(_BACKOFF_BASE * (2 ** (retries - 1)), _BACKOFF_CAP)
        self.notify(f"🔁 {reason}重连第 {retries} 次，{delay} 秒后重试："
                    f"{live.anchor or live.room_url}")
        # 用 Event.wait 代替 sleep，停止指令能立刻中断等待
        self._stop.wait(delay)
        return not self._stop.is_set()

    def _fresh_live(self, live):
        """重新探测房间状态，失败返回 None。"""
        fresh = self._probe(live.room_url)
        return fresh if fresh and fresh.is_live and fresh.stream_url else None

    def _progress_reporter(self, live):
        """把 ffmpeg 进度节流成每 60 秒一条通知。"""
        state = {"last": time.monotonic()}

        def report(seconds: float, size: int) -> None:
            now = time.monotonic()
            if now - state["last"] < 60:
                return
            state["last"] = now
            self.notify(f"🎥 录制中 {live.anchor or live.room_url}："
                        f"{_fmt_duration(seconds)} / {size / 1024 / 1024:.1f} MB")

        return report

    def _probe(self, room_url: str):
        adapter = find_adapter(room_url, self.cookies, proxy=self.config.proxy)
        if adapter is None:
            logger.error("无法识别的直播间链接: %s", room_url)
            return None
        try:
            return adapter.get_live_info(room_url)
        except Exception as exc:
            logger.error("探测直播间失败 %s: %s", room_url, exc)
            return None


def _fmt_duration(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
