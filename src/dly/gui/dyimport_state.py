"""抖音「喜欢 / 收藏」批量导入的界面状态管理。

把 CDP 会话、扫描线程、进度与结果集中在一处，server.py 只做薄路由。
所有耗时操作都在后台线程，HTTP 接口立即返回。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dly.core import douyin_import as di
from dly.core.paths import data_dir

logger = logging.getLogger(__name__)


class DyImportState:
    def __init__(self, log: Callable[[str, str, str], None]) -> None:
        self._log = log
        self._lock = threading.RLock()
        self._session: Optional[di.ChromeSession] = None
        self._stop = threading.Event()
        self._busy = False
        self.profile_dir = data_dir() / "dy_import_profile"
        # 前端轮询这个快照
        self.snapshot: Dict[str, Any] = {
            "chrome": bool(di.find_chrome()),
            "logged_in": False,
            "checking": False,
            "scanning": False,
            "tab": "",
            "found": 0,
            "items": [],
            "error": "",
        }

    # -- 查询 -------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.snapshot, items=list(self.snapshot["items"]))

    def _set(self, **kwargs: Any) -> None:
        with self._lock:
            self.snapshot.update(kwargs)

    # -- 会话 -------------------------------------------------------------

    def _ensure_session(self) -> di.ChromeSession:
        if self._session is None:
            chrome = di.find_chrome()
            if not chrome:
                raise RuntimeError("没找到 Chrome 或 Edge，请先安装一个再使用此功能")
            self._session = di.ChromeSession(self.profile_dir, chrome)
        return self._session

    def start_login(self) -> None:
        """启动可见 Chrome，让用户在专用 profile 里扫码登录抖音。"""
        with self._lock:
            if self._busy:
                raise RuntimeError("正在处理中，请稍候")
            self._busy = True
        self._set(error="", checking=True, logged_in=False)

        def work() -> None:
            s = None
            try:
                s = self._ensure_session()
                s.start(url=di._PROFILE_URL, headless=False)
                self._log("download", "已打开抖音登录窗口，请用手机抖音扫码登录（仅这一次）")
                ok = di.wait_login(s, tick=lambda: None)
                self._set(logged_in=bool(ok))
                if ok:
                    self._log("download", "抖音登录成功，登录态已保存，下次不用再扫码")
                else:
                    self._set(error="登录超时，请重试")
            except Exception as exc:  # noqa: BLE001
                logger.exception("抖音登录失败")
                self._set(error=str(exc))
            finally:
                # 登录窗完成使命就关掉；收集阶段会另起 headless 实例
                if s:
                    s.close()
                self._set(checking=False)
                with self._lock:
                    self._session = None
                    self._busy = False

        threading.Thread(target=work, daemon=True).start()

    def check_login(self) -> None:
        """后台探测专用 profile 是否已有登录态（不弹窗）。"""
        with self._lock:
            if self._busy:
                return
            self._busy = True
        self._set(checking=True, error="")

        def work() -> None:
            s = None
            try:
                s = self._ensure_session()
                s.start(headless=True)
                self._set(logged_in=s.is_logged_in())
            except Exception as exc:  # noqa: BLE001
                logger.debug("抖音登录态探测失败: %s", exc)
                self._set(logged_in=False)
            finally:
                if s:
                    s.close()
                self._set(checking=False)
                with self._lock:
                    self._session = None
                    self._busy = False

        threading.Thread(target=work, daemon=True).start()

    # -- 扫描 -------------------------------------------------------------

    def start_scan(self, tabs: List[str]) -> None:
        """扫描指定列表；tabs 为 like / favorite 的子集。"""
        tabs = [t for t in tabs if t in ("like", "favorite")]
        with self._lock:
            if self._busy or not tabs:
                raise RuntimeError("正在处理中或未选择列表")
            self._busy = True
            self._stop.clear()
        self._set(scanning=True, found=0, items=[], error="", tab="")

        def work() -> None:
            s = None
            try:
                s = self._ensure_session()
                s.start(headless=True)
                if not s.is_logged_in():
                    raise RuntimeError("抖音登录已失效，请重新扫码登录")
                collected: Dict[str, dict] = {}
                for tab in tabs:
                    if self._stop.is_set():
                        break
                    label = "喜欢" if tab == "like" else "收藏"
                    self._set(tab=label)
                    self._log("download", f"开始扫描抖音「{label}」列表…")

                    def on_item(item: dict) -> None:
                        collected[item["id"]] = item
                        self._set(items=list(collected.values()), found=len(collected))

                    result = di.collect(s, tab, on_item, self._stop.is_set)
                    self._log(
                        "download",
                        f"「{label}」扫到 {result['found']} 条"
                        + ("（已停止）" if result["stopped"] else ""),
                    )
                self._set(scanning=False)
                self._log("download", f"共收集 {len(collected)} 条，勾选后可以导入下载")
            except Exception as exc:  # noqa: BLE001
                logger.exception("抖音列表扫描失败")
                self._set(error=str(exc), scanning=False)
            finally:
                if s:
                    s.close()
                self._set(scanning=False)
                with self._lock:
                    self._session = None
                    self._busy = False

        threading.Thread(target=work, daemon=True).start()

    def stop_scan(self) -> None:
        self._stop.set()
        self._set(scanning=False)
