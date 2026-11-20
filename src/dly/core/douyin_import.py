"""从抖音「喜欢 / 收藏」批量收集视频列表。

设计取舍（见 2026-11-20 工作日志）：
- 不走纯 HTTP 接口：抖音 Web 列表接口需要 ``x-secsdk-web-signature`` / ``a_bogus`` /
  ``msToken`` 等动态签名，其中 secsdk 由页面 JS SDK 生成、项目没有对应实现，逆向成本与失效
  风险都高。改为「浏览器内收集」：在真实登录的 Chrome 页面里滚动，拦截列表接口响应取 id，
  签名与风控全部交给页面自己处理。
- 不引入 puppeteer / playwright：用轻量 CDP 客户端（websocket-client）直连系统 Chrome，
  避免把 Node 运行时打进安装包。

对外主要入口：
    - ``find_chrome()``        定位系统 Chrome
    - ``ChromeSession``       管理专用 profile + CDP 连接
    - ``collect(session, tab, on_item, should_stop)``  滚动收集一个列表
    - ``video_url(aweme_id)``  拼出可交给现有下载管线的链接
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# 列表接口 URL 片段 -> 逻辑 tab 名
_API_TABS = {
    "aweme/favorite": "like",
    "aweme/listcollection": "favorite",
}

# 登录判定 cookie（抖音 Web 登录态关键项）
_LOGIN_COOKIES = ("sessionid", "sessionid_ss", "sid_tt")

# 用户主页
_PROFILE_URL = "https://www.douyin.com/user/self"

# 每次滚动等待（秒）
_SCROLL_WAIT = 1.6


# ---------------------------------------------------------------------------
# Chrome 定位
# ---------------------------------------------------------------------------

def find_chrome() -> Optional[Path]:
    """定位系统 Chrome 可执行文件（注册表优先，再看常见路径与 PATH）。"""
    candidates: List[Path] = []
    if os.name == "nt":
        try:
            import winreg
            for root, flags in ((winreg.HKEY_LOCAL_MACHINE, 0),
                                (winreg.HKEY_CURRENT_USER, 0)):
                for key in (
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                ):
                    try:
                        with winreg.OpenKey(root, key, 0, winreg.KEY_READ | flags) as h:
                            val, _ = winreg.QueryValueEx(h, "")
                            if val:
                                candidates.append(Path(val.strip('"')))
                    except OSError:
                        pass
        except ImportError:
            pass
        for base in (
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if base:
                candidates += [
                    Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
                    Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                ]
    for p in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(p)
        if found:
            candidates.append(Path(found))
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def _free_port() -> int:
    """向系统要一个空闲 TCP 端口作为 CDP 调试端口。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def video_url(aweme_id: str) -> str:
    """把作品 id 拼成现有解析/下载管线可识别的链接。"""
    return f"https://www.douyin.com/video/{aweme_id}"


# ---------------------------------------------------------------------------
# 轻量 CDP 客户端
# ---------------------------------------------------------------------------

class CDP:
    """极简 Chrome DevTools Protocol 客户端（只需本项目用到的几个域）。"""

    def __init__(self, ws_url: str, timeout: float = 20.0):
        import websocket  # 延迟导入，未安装时只在真正使用时报错
        self._ws = websocket.create_connection(
            ws_url, timeout=timeout, suppress_origin=True
        )
        self._seq = 0
        self.events: List[dict] = []

    def send(self, method: str, params: Optional[dict] = None) -> dict:
        self._seq += 1
        mid = self._seq
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == mid:
                return msg.get("result", {})
            self.events.append(msg)

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        """只发命令不等响应（Browser.close 之后浏览器不会再回消息）。"""
        self._seq += 1
        self._ws.send(json.dumps({
            "id": self._seq, "method": method, "params": params or {}
        }))

    def drain(self, seconds: float) -> None:
        """在 seconds 内持续接收事件（CDP 的响应通知是异步事件）。"""
        end = time.time() + seconds
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                break
            self._ws.settimeout(remaining)
            try:
                self.events.append(json.loads(self._ws.recv()))
            except Exception:
                break
        self._ws.settimeout(20)

    def evaluate(self, expression: str) -> object:
        r = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        return r.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


class ChromeSession:
    """管理一个带独立 profile 的 Chrome 进程及其 CDP 连接。

    独立 profile 让登录态与用户日常浏览器隔离，并持久化在 ``profile_dir``，
    登录一次之后可反复复用。
    """

    def __init__(self, profile_dir: Path, chrome: Optional[Path] = None):
        self.profile_dir = Path(profile_dir)
        self.chrome = chrome or find_chrome()
        self.port = _free_port()
        self.proc: Optional[subprocess.Popen] = None
        self.cdp: Optional[CDP] = None

    # -- 生命周期 ---------------------------------------------------------

    def _base_cmd(self) -> List[str]:
        assert self.chrome is not None, "未找到 Chrome/Edge，请先安装"
        return [
            str(self.chrome),
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
        ]

    def is_logged_in(self) -> bool:
        """判断专用 profile 是否真的登录了抖音。

        只看 cookie 名字会误判：过期/残留的 sessionid 仍在，但页面其实是
        「未登录」，列表接口一条也拿不到。这里再看一眼当前页面的登录文案。
        """
        if not self.profile_dir.exists() or not self._cookies_have_login():
            return False
        if not self.cdp:
            return False
        # 刚启动时页面可能还没渲染完；等到有内容再判断，空页面一律算未登录。
        for _ in range(12):
            try:
                text = self.cdp.evaluate(
                    "document.body ? document.body.innerText.slice(0, 3000) : ''"
                )
                if text and len(str(text).strip()) > 10:
                    return not any(mark in str(text) for mark in (
                        "登录后即可观看", "扫码登录", "验证码登录", "未登录",
                    ))
            except Exception:
                logger.debug("读取页面登录状态失败", exc_info=True)
            time.sleep(0.5)
        return False

    def _cookies_have_login(self) -> bool:
        # 需要进程在跑才有 CDP；这里由调用方确保 start() 已执行
        if not self.cdp:
            return False
        try:
            # 从 document.cookie 读不到 httponly 的 sessionid，改用 CDP Network 域
            self.cdp.send("Network.enable")
            cookies = self.cdp.send("Network.getCookies", {"urls": ["https://www.douyin.com/"]})
            names = {c.get("name") for c in cookies.get("cookies", [])}
            return any(n in names for n in _LOGIN_COOKIES)
        except Exception as exc:
            logger.debug("读取抖音 cookie 失败: %s", exc)
            return False

    def start(self, url: str = _PROFILE_URL, headless: bool = False) -> None:
        """启动 Chrome 并连接到抖音页面。

        headless=False 用于首次扫码登录（用户需要看到页面）；已登录后的收集阶段用 True。
        """
        if self.proc and self.proc.poll() is None:
            return
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        cmd = self._base_cmd()
        if headless:
            cmd.append("--headless=new")
        cmd.append(url)
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x8)
        self.proc = subprocess.Popen(
            cmd, creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ws = self._wait_for_page()
        self.cdp = CDP(ws)

    def _wait_for_page(self, timeout: float = 25.0) -> str:
        """等 CDP 端点就绪并返回抖音页面的 websocket 地址。"""
        end = time.time() + timeout
        last_err: Exception = Exception("timeout")
        while time.time() < end:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json", timeout=2
                ) as r:
                    targets = json.load(r)
                for t in targets:
                    if t.get("type") == "page" and "douyin.com" in t.get("url", ""):
                        return t["webSocketDebuggerUrl"]
            except Exception as exc:
                last_err = exc
            time.sleep(0.4)
        raise RuntimeError(f"Chrome CDP 未就绪: {last_err}")

    def ensure_page(self) -> None:
        """确保 CDP 连着抖音页面（导航后 target 可能换 ws 地址）。"""
        if self.cdp:
            return
        ws = self._wait_for_page()
        self.cdp = CDP(ws)

    def close(self, kill: bool = True) -> None:
        if self.cdp:
            try:
                # 必须让 Chrome 自己走正常退出流程，否则 Cookie 还在内存里
                # 就被 terminate 掉，登录当下显示成功、下次启动却提示未登录。
                self.cdp.notify("Browser.close")
                if self.proc and self.proc.poll() is None:
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            except Exception:  # noqa: BLE001
                logger.debug("Browser.close 未完成，走强制收尾", exc_info=True)
            self.cdp.close()
            self.cdp = None
        if kill and self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None


# ---------------------------------------------------------------------------
# 列表收集
# ---------------------------------------------------------------------------

def _find_items(obj: object, out: Dict[str, dict]) -> None:
    """递归从接口响应里提取作品摘要（响应结构较深，按字段扫描最稳）。"""
    if isinstance(obj, list):
        for item in obj:
            _find_items(item, out)
        return
    if not isinstance(obj, dict):
        return
    aid = obj.get("aweme_id")
    if isinstance(aid, (str, int)) and str(aid).isdigit():
        key = str(aid)
        if key not in out:
            out[key] = {
                "id": key,
                "desc": (obj.get("desc") or "")[:120],
                "author": ((obj.get("author") or {}).get("nickname") or ""),
                "cover": _cover_of(obj),
            }
    for v in obj.values():
        _find_items(v, out)


def _cover_of(item: dict) -> str:
    video = item.get("video") or {}
    cover = video.get("cover") or item.get("cover") or {}
    urls = cover.get("url_list") or []
    return urls[0] if urls else ""


def _decode_body(body_info: dict) -> str:
    raw = body_info.get("body", "")
    if body_info.get("base64Encoded"):
        try:
            return base64.b64decode(raw).decode("utf-8", "ignore")
        except Exception:
            return ""
    return raw


def collect(
    session: ChromeSession,
    tab: str,
    on_item: Callable[[dict], None],
    should_stop: Callable[[], bool],
    max_scrolls: int = 400,
    idle_limit: int = 6,
) -> dict:
    """滚动收集一个列表，边扫边通过 ``on_item`` 回调新发现的作品。

    tab: ``like``（喜欢）或 ``favorite``（收藏）。
    返回 ``{"tab":..., "found":..., "scrolls":..., "stopped":...}``。
    """
    assert tab in ("like", "favorite"), f"未知 tab: {tab}"
    cdp = session.cdp
    if cdp is None:
        raise RuntimeError("浏览器未连接")

    cdp.events.clear()
    cdp.send("Network.enable")

    # 直接导航到对应 tab（部分版本 showTab 生效，不生效则回退到点击）
    show_tab = "like" if tab == "like" else "favorite_collection"
    cdp.send("Page.navigate", {"url": f"{_PROFILE_URL}?showTab={show_tab}"})
    cdp.drain(4.5)

    # 若 URL 没带上 tab，真实点击 tab
    label = "喜欢" if tab == "like" else "收藏"
    cur = cdp.evaluate("location.href") or ""
    if show_tab not in str(cur):
        # evaluate 不带参数，直接把 label 内联进表达式
        expr = ("(() => { const label = %s; "
                "const el = [...document.querySelectorAll('span,div')].find("
                "e => e.children.length === 0 && e.textContent.trim() === label); "
                "if (!el) return null; const r = el.getBoundingClientRect(); "
                "return {x: r.x + r.width/2, y: r.y + r.height/2}; })()") % json.dumps(label, ensure_ascii=False)
        box = cdp.evaluate(expr)
        if box:
            for t in ("mousePressed", "mouseReleased"):
                cdp.send("Input.dispatchMouseEvent", {
                    "type": t, "x": box["x"], "y": box["y"],
                    "button": "left", "clickCount": 1,
                })
            cdp.drain(4)

    seen: Set[str] = set()
    scrolls = 0
    idle = 0
    stopped = False

    # 首屏响应
    def harvest_events() -> int:
        added = 0
        for ev in cdp.events:
            if ev.get("method") != "Network.responseReceived":
                continue
            resp = ev.get("params", {}).get("response", {})
            url = resp.get("url", "")
            matched = next((v for k, v in _API_TABS.items() if k in url), None)
            if matched != tab:
                continue
            rid = ev.get("params", {}).get("requestId")
            if not rid:
                continue
            try:
                info = cdp.send("Network.getResponseBody", {"requestId": rid})
                data = json.loads(_decode_body(info) or "{}")
            except Exception:
                continue
            items: Dict[str, dict] = {}
            _find_items(data.get("aweme_list") or data, items)
            for key, item in items.items():
                if key not in seen:
                    seen.add(key)
                    on_item(item)
                    added += 1
        cdp.events.clear()
        return added

    harvest_events()

    while scrolls < max_scrolls:
        if should_stop():
            stopped = True
            break
        before = len(seen)
        # 滚动到底部触发下一页；同时尝试内部滚动容器
        cdp.evaluate(
            "window.scrollTo(0, document.body.scrollHeight);"
            "document.querySelectorAll('*').forEach(e => {"
            "if (e.scrollHeight > e.clientHeight + 200 && e.clientHeight > 300) "
            "e.scrollTop = e.scrollHeight;});"
        )
        cdp.drain(_SCROLL_WAIT)
        harvest_events()
        scrolls += 1
        if len(seen) == before:
            idle += 1
            if idle >= idle_limit:
                break
        else:
            idle = 0
        time.sleep(0.15)

    return {"tab": tab, "found": len(seen), "scrolls": scrolls, "stopped": stopped}


def wait_login(session: ChromeSession, timeout: float = 300.0,
               tick: Callable[[], None] = lambda: None) -> bool:
    """在可见窗口里等待用户扫码登录；登录态写入专用 profile 后返回。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        tick()
        try:
            # 页面可能在登录中转页之间跳转，用页面真实状态判断
            session.ensure_page()
            if session.is_logged_in():
                return True
        except Exception:
            pass
        time.sleep(2)
    return False
