"""汽水音乐分享链接采集的界面状态管理。

跟 DyImportState 一样：HTTP 接口立即返回，耗时解析/下载放后台线程，
前端轮询 snapshot 看进度和结果。采集完直接进 DLY 歌单，封面一起写进去。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
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
        self._login_stop = threading.Event()
        self._sessionid = ""
        self._qr_cookie = ""
        self._qr_token = ""
        # 登录态只存本地数据目录，不上传、不进前端快照
        self._auth_file = data_dir() / "qishui_auth.json"
        self._load_auth()
        self.snapshot: Dict[str, Any] = {
            "logged_in": bool(self._sessionid),
            "qrcode": "",
            "qr_expire": 0,
            "qr_hint": "",
            "qr_last_status": "",
            "qr_got_credential": False,
            "playlists": [],
            "busy": False,
            "stage": "",
            "done": 0,
            "total": 0,
            "title": "",
            "kind": "",
            "note": "",
            "items": [],
            "error": "",
            "added": 0,
        }

    # -- 登录态 -----------------------------------------------------------

    def _load_auth(self) -> None:
        try:
            data = json.loads(self._auth_file.read_text(encoding="utf-8"))
            self._sessionid = str(data.get("sessionid") or "")
        except (OSError, ValueError):
            self._sessionid = ""

    def _save_auth(self) -> None:
        try:
            self._auth_file.parent.mkdir(parents=True, exist_ok=True)
            self._auth_file.write_text(
                json.dumps({"sessionid": self._sessionid}, ensure_ascii=False),
                encoding="utf-8")
        except OSError as exc:
            logger.debug("保存汽水登录态失败: %s", exc)

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

    # -- 扫码登录 ---------------------------------------------------------

    def start_login(self) -> None:
        """取二维码，开后台线程轮询扫码结果。"""
        qr = qi.auth_qrcode()
        self._qr_token = qr["token"]
        self._qr_cookie = qr.get("cookie", "")
        self._login_stop.clear()
        self._set(qrcode=qr["qrcode"], qr_expire=qr.get("expire_at", 0),
                  qr_hint="请用抖音 APP 扫码并确认", logged_in=False)
        self._log("汽水音乐", "二维码已生成，请用抖音 APP 扫码", "info")

        def poll() -> None:
            deadline = time.time() + max(60, qr.get("expire_at", 0) - time.time())
            while time.time() < deadline and not self._login_stop.is_set():
                try:
                    r = qi.auth_qrcode_status(self._qr_token, self._qr_cookie)
                    # 只记状态和「是否拿到凭证」，绝不把 sessionid 写进日志
                    self._set(qr_last_status=r.get("status", ""),
                              qr_got_credential=bool(r.get("sessionid")))
                    if r.get("sessionid"):
                        self._sessionid = r["sessionid"]
                        self._save_auth()
                        self._set(logged_in=True, qrcode="", qr_expire=0)
                        self._log("汽水音乐", "登录成功", "ok")
                        return
                    status = r.get("status")
                    if status in ("expired", "canceled", "cancelled", "5", "4"):
                        self._set(qrcode="", qr_expire=0,
                                  error="二维码已失效，请重新获取")
                        self._log("汽水音乐", "二维码已失效", "err")
                        return
                    if status in ("scanned", "2"):
                        self._set(qr_hint="已扫码，请在手机上确认")
                        self._log("汽水音乐", "已扫码，请在手机上确认", "info")
                    elif status in ("success", "3", "confirmed"):
                        self._set(qr_hint="已确认，正在获取登录态…")
                        self._log("汽水音乐", "已确认，正在获取登录态…", "info")
                except Exception as exc:
                    logger.warning("轮询汽水扫码状态失败: %s", exc)
                    self._set(qr_hint="轮询失败：" + str(exc)[:60])
                time.sleep(1.5)
            if not self._login_stop.is_set():
                self._set(qrcode="", qr_expire=0)
                self._log("汽水音乐", "扫码超时，请重新获取二维码", "err")

        threading.Thread(target=poll, daemon=True).start()

    def cancel_login(self) -> None:
        self._login_stop.set()
        self._set(qrcode="", qr_expire=0)

    def logout(self) -> None:
        self._sessionid = ""
        self._save_auth()
        self._set(logged_in=False, playlists=[])
        self._log("汽水音乐", "已退出登录", "info")

    def login_with_cookie(self, raw: str) -> None:
        """扫码失败时的兜底：直接粘浏览器 Cookie，从中取 sessionid。"""
        cookie = (raw or "").strip()
        if not cookie:
            raise ValueError("先粘贴 Cookie")
        sid = ""
        for name in ("sessionid", "sessionid_ss", "sid_tt", "sid_guard"):
            m = re.search(r"(?:^|[;\s])" + name + r"=([^;\s]+)", cookie, re.I)
            if m:
                sid = m.group(1)
                break
        if not sid:
            raise ValueError("Cookie 里没有找到 sessionid 等登录凭证")
        self._sessionid = sid
        self._save_auth()
        self._set(logged_in=True, qrcode="", qr_expire=0, error="")
        self._log("汽水音乐", "已从 Cookie 登录", "ok")

    def load_playlists(self) -> List[Dict[str, str]]:
        """拉我的歌单列表（含「我喜欢」），失败时给出明确错误。"""
        if not self._sessionid:
            raise RuntimeError("请先扫码登录")
        try:
            pls = qi.me_playlists(self._sessionid)
        except Exception as exc:
            self._set(error="拉取歌单失败：" + str(exc)[:160])
            raise
        self._set(playlists=pls)
        return pls

    def start_from_playlist(self, playlist_id: str, limit: int = 0) -> None:
        """从登录态歌单 ID 采集：走和链接采集完全一样的后台流程。"""
        if not self._sessionid:
            raise RuntimeError("请先扫码登录")
        self._start_common({"kind": "playlist", "value": playlist_id},
                           limit=limit, output_dir="")

    def _start_common(self, parsed: dict, limit: int = 0,
                      output_dir: str = "") -> None:
        """两个入口共用：检查 busy → 清空旧结果 → 后台解析下载。"""
        with self._lock:
            if self._busy:
                raise RuntimeError("正在采集中，请稍候")
            self._busy = True
        self._stop.clear()
        self._set(busy=True, error="", items=[], done=0, total=0,
                  stage="解析链接", title="", kind="", note="", added=0)

        def work() -> None:
            try:
                def progress(stage: str, cur: int, total: int) -> None:
                    self._set(stage=stage, total=total)
                    if cur <= 3 or cur % 20 == 0:
                        self._log("汽水音乐", f"{stage} {cur}/{total}", "info")

                result = qi.collect(parsed, on_progress=progress, limit=limit)
                self._run_download(result.title, result.kind, result.note,
                                   list(result.tracks), output_dir)
            except Exception as exc:  # noqa: BLE001
                logger.exception("汽水采集失败")
                self._set(error=str(exc)[:300], stage="失败", busy=False)
                self._log("汽水音乐", f"采集失败：{exc}", "err")
            finally:
                with self._lock:
                    self._busy = False

        threading.Thread(target=work, daemon=True).start()

    # -- 采集 -------------------------------------------------------------

    def start(self, text: str, output_dir: str = "", limit: int = 0) -> None:
        self._start_common(qi.parse_share(text), limit=limit,
                           output_dir=output_dir)

    def _run_download(self, title: str, kind: str, note: str,
                      tracks: List[qi.QishuiTrack], output_dir: str) -> None:
        """下载 + 入库 + 写歌词。两种采集入口（分享链接 / 我的歌单）共用。"""
        try:
            self._set(title=title, kind=kind, note=note,
                      total=len(tracks), done=0, stage="准备下载")
            self._log("汽水音乐", f"解析到《{title}》共 {len(tracks)} 首", "ok")

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
                    # 歌词写成同目录 .lrc，Library 会按音频名找它
                    if t.lyric:
                        try:
                            lrc_path = got.audio.with_suffix(".lrc")
                            lrc_path.write_text(
                                qi.build_lrc(t.lyric, t.clip_duration or t.duration),
                                encoding="utf-8")
                        except OSError as exc:
                            logger.debug("写歌词失败 %s: %s", t.id, exc)
                    added += 1
                    self._item(i - 1, status="已入库",
                               library_id=track["id"],
                               file=got.audio.name)
                except Exception as exc:  # 单首失败继续下一首
                    logger.debug("汽水曲目入库失败 %s: %s", t.id, exc)
                    self._item(i - 1, status="失败", error=str(exc)[:160])
                finally:
                    self._set(done=i)

            self._set(added=added, stage="完成", busy=False)
            self._log("汽水音乐", f"采集完成，已入库 {added} 首", "ok")
        except Exception as exc:  # noqa: BLE001
            logger.exception("汽水下载失败")
            self._set(error=str(exc)[:300], stage="失败", busy=False)
            self._log("汽水音乐", f"采集失败：{exc}", "err")
        finally:
            with self._lock:
                self._busy = False
