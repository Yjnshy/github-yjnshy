"""汽水音乐分享链接采集的界面状态管理。

跟 DyImportState 一样：HTTP 接口立即返回，耗时解析/下载放后台线程，
前端轮询 snapshot 看进度和结果。采集完直接进 DLY 歌单，封面一起写进去。
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dly.core import qishui_import as qi
from dly.core.library import Library
from dly.core.paths import data_dir

logger = logging.getLogger(__name__)


class QishuiState:
    def __init__(self, log: Callable[[str, str, str], None],
                 library: Library) -> None:
        self._log = log
        self._library = library
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._busy = False
        self.snapshot: Dict[str, Any] = {
            "busy": False,
            "stage": "",
            "done": 0,
            "total": 0,
            "title": "",
            "note": "",
            "items": [],
            "error": "",
            "added": 0,
        }

    # -- 查询 -------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.snapshot, items=list(self.snapshot["items"]))

    def _set(self, **kwargs: Any) -> None:
        with self._lock:
            self.snapshot.update(kwargs)

    def _item(self, index: int, **kwargs: Any) -> None:
        with self._lock:
            items = self.snapshot["items"]
            while len(items) <= index:
                items.append({})
            items[index].update(kwargs)

    def stop(self) -> None:
        self._stop.set()
        self._log("汽水音乐", "已请求停止采集", "info")

    # -- 采集 -------------------------------------------------------------

    def start(self, text: str, output_dir: str = "", limit: int = 0) -> None:
        with self._lock:
            if self._busy:
                raise RuntimeError("正在采集中，请稍候")
            self._busy = True
        self._stop.clear()
        self._set(busy=True, error="", items=[], done=0, total=0,
                  stage="解析链接", title="", note="", added=0)

        def work() -> None:
            try:
                parsed = qi.parse_share(text)
                self._set(stage="读取歌单")

                def progress(stage: str, cur: int, total: int) -> None:
                    self._set(stage=stage, total=total)
                    if cur <= 3 or cur % 20 == 0:
                        self._log("汽水音乐", f"{stage} {cur}/{total}", "info")

                result = qi.collect(parsed, on_progress=progress)
                tracks: List[qi.QishuiTrack] = result.tracks
                if limit and limit > 0:
                    tracks = tracks[:limit]
                self._set(title=result.title, note=result.note,
                          total=len(tracks), done=0, stage="准备下载")
                self._log("汽水音乐",
                          f"解析到《{result.title}》共 {len(tracks)} 首", "ok")

                out = Path(output_dir).expanduser() if output_dir else \
                    data_dir() / "qishui"
                out.mkdir(parents=True, exist_ok=True)

                added = 0
                for i, t in enumerate(tracks, 1):
                    if self._stop.is_set():
                        self._log("汽水音乐", "采集已停止", "info")
                        break
                    self._set(stage="下载", done=i - 1)
                    self._item(i - 1, title=t.title, artist=t.artist,
                               status="下载中", id=t.id)
                    try:
                        got = qi.download(t, out)
                        self._item(i - 1, status="入库中")
                        # 直接进歌单；封面用汽水的原图覆盖默认渐变封面
                        track = self._library.add(
                            got.audio, title=t.title, cover_src=got.cover,
                            source="qishui")
                        if got.cover:
                            self._library.set_cover(track["id"], got.cover)
                            t.cover_url = ""     # 已落地，不用再显示外链
                        added += 1
                        self._item(i - 1, status="已入库",
                                   library_id=track["id"],
                                   file=got.audio.name)
                    except Exception as exc:  # 单首失败继续下一首
                        logger.debug("汽水曲目入库失败 %s: %s", t.id, exc)
                        self._item(i - 1, status="失败", error=str(exc)[:160])
                    finally:
                        self._set(done=i)

                self._set(added=added, stage="完成")
                self._log("汽水音乐", f"采集完成，已入库 {added} 首", "ok")
            except Exception as exc:  # noqa: BLE001
                logger.exception("汽水采集失败")
                self._set(error=str(exc)[:300], stage="失败")
                self._log("汽水音乐", f"采集失败：{exc}", "err")
            finally:
                with self._lock:
                    self._busy = False
                self._set(busy=False)

        threading.Thread(target=work, daemon=True).start()
