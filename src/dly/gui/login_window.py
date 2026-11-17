"""登录窗：在自己开的窗口里登录，直接把会话交给程序。

为什么必须有这个东西
--------------------
Edge / 新版 Chrome 给 Cookie 加了 App-Bound Encryption，密钥绑在浏览器
进程上，第三方程序解不开——这是浏览器厂商的安全设计，绕不过去。让用户
「去浏览器登录然后导出 cookies.txt」对普通用户等于劝退。

这里改成：程序开一个 WebView2 窗口，加载真实的站点页面，用户像平常那样
扫码/输账号，登录成功后 WebView2 把该站点的 Cookie 直接交给同进程的
Python 代码——**不涉及解密，也不碰用户浏览器里的数据**。

为什么是独立进程
----------------
``webview.start()`` 必须占用主线程，而主程序窗口已经占着了，所以由主程序
用 ``--login <平台>`` 再拉起一个自己（打包版就是同一个 exe），登录完写盘
退出，主程序轮询文件即可。这样也不会把两个窗口的生命周期搅在一起。

对用户的说法里不出现 Cookie / 会话 / 凭证 等词：界面上只叫「登录」。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from dly.cookies import sessions

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.2          # 轮询登录状态的间隔
SAVE_SETTLE_SECONDS = 1.0   # 检测到登录后略等一下，让 Cookie 落全
WARMUP_SECONDS = 2.0        # 开工前等 WebView2 初始化
MAX_WAIT_SECONDS = 600      # 十分钟没登录就自己关掉

EXIT_OK = 0
EXIT_CANCELLED = 2
EXIT_TIMEOUT = 3


@dataclass(frozen=True)
class Site:
    """一个可登录的站点。"""

    key: str
    url: str
    # 只有登录后才会出现的 Cookie 名：用它判断「登没登」
    markers: tuple
    window_title: str


SITES: Dict[str, Site] = {
    "bilibili": Site(
        "bilibili", "https://www.bilibili.com",
        ("SESSDATA",),
        "登录 B 站 — 登录后可下载高清与会员内容"),
    "douyin": Site(
        "douyin", "https://www.douyin.com",
        ("sessionid", "sessionid_ss", "sid_tt"),
        "登录抖音 — 登录后可下载完整作品"),
    "xiaohongshu": Site(
        "xiaohongshu", "https://www.xiaohongshu.com",
        ("web_session",),
        "登录小红书 — 登录后可下载图文与视频"),
}


def site_of(platform: str) -> Optional[Site]:
    return SITES.get((platform or "").lower())


def _debug_paths() -> "list[Path]":
    """诊断日志的落点：程序目录 + 会话文件旁，去重后逐个试。"""
    out = []
    for path in (Path(sys.executable).resolve().parent / "login_debug.log",
                 sessions.STORE_PATH.parent / "login_debug.log"):
        if path not in out:
            out.append(path)
    return out


def _debug(message: str, *args) -> None:
    """登录窗的诊断日志。

    打包版没有控制台，用户「登了但没生效」时只能靠这个文件定位。
    只记状态与 Cookie 名字，不记值。
    """
    text = message % args if args else message
    logger.info(text)
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n"
    errors = []
    for path in _debug_paths():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            errors.append(f"{path} -> {exc!r}")
    if errors:
        # 一条日志都写不进去时才落地错误本身，避免静默失联
        try:
            probe = _debug_paths()[0].parent / "login_debug_err.txt"
            probe.write_text("\n".join(errors) + "\n", encoding="utf-8")
        except OSError:
            pass


def _iter_cookies(cookies) -> "list[tuple]":
    """把各后端返回的 Cookie 归一成 ``(name, value, domain)``。

    pywebview 的 WebView2 后端返回的是 ``http.cookies.SimpleCookie``（每个对象
    只装一条），旧版本/其他后端返回 ``{'name': ..., 'value': ...}`` 字典，
    两种都得认，否则一条都读不到。
    """
    out = []
    for item in cookies or []:
        if isinstance(item, dict) and "name" in item:
            out.append((item.get("name") or "", item.get("value") or "",
                        item.get("domain") or ""))
            continue
        items = getattr(item, "items", None)
        if items is None:
            continue
        try:
            pairs = list(items())
        except TypeError:
            continue
        for name, morsel in pairs:
            value = getattr(morsel, "value", "")
            domain = ""
            try:
                domain = morsel.get("domain") or ""
            except AttributeError:
                domain = ""
            out.append((name, value, domain))
    return out


def _cookie_domain_match(cookie_domain: str, host: str) -> bool:
    """Cookie 的 domain 是否属于该站点。"""
    d = (cookie_domain or "").lstrip(".").lower()
    h = host.lstrip(".").lower()
    return bool(d) and (d == h or d.endswith("." + h) or h.endswith("." + d))


def _header_from_cookies(cookies, host: str) -> str:
    parts = []
    for name, value, domain in _iter_cookies(cookies):
        if not name or not _cookie_domain_match(domain, host):
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _has_marker(cookies, host: str, markers: tuple) -> bool:
    for name, value, domain in _iter_cookies(cookies):
        if name in markers and value and _cookie_domain_match(domain, host):
            return True
    return False


def run(platform: str) -> int:
    """打开登录窗并阻塞到结束，返回进程退出码。"""
    _debug("启动登录窗：platform=%r 用户=%s 家目录=%s 会话文件=%s",
           platform, os.environ.get("USERNAME", "?"), Path.home(),
           sessions.STORE_PATH)
    site = site_of(platform)
    if site is None:
        logger.error("未知的登录目标：%s", platform)
        return EXIT_CANCELLED

    from dly.gui import clr_boot     # 打包版要先切到 coreclr
    from dly.gui.desktop import DesktopUnavailable

    clr_boot.prepare()
    try:
        import webview
    except ImportError:              # pragma: no cover - 缺依赖的环境
        logger.error("缺少 pywebview，无法打开登录窗")
        return EXIT_CANCELLED

    from urllib.parse import urlparse

    host = urlparse(site.url).netloc
    window = webview.create_window(
        site.window_title, site.url,
        width=480, height=760,
        min_size=(400, 600),
        text_select=True,
    )

    state = {"saved": False, "done": False,
             "final_check": False, "closing_once": False}

    def _grab(timeout: float = 8.0) -> dict:
        """取一次 Cookie；用看门狗避免某次调用吊死整个循环。

        ``get_cookies()`` 内部是 ``Invoke`` + 信号量等待，页面还没就绪时
        有可能很久不返回，这里放在子线程里带超时地等。
        """
        box: dict = {}

        def call() -> None:
            try:
                box["cookies"] = window.get_cookies() or []
            except Exception as exc:      # noqa: BLE001 取不到就等下一轮
                box["error"] = exc

        thread = threading.Thread(target=call, daemon=True, name="login-grab")
        thread.start()
        thread.join(timeout)
        return box

    def _try_save(prefix: str = "") -> bool:
        box = _grab()
        if "cookies" not in box:
            _debug("%s取登录状态未返回（%r）", prefix,
                   type(box.get("error")).__name__ if box.get("error") else "超时")
            return False
        cookies = box["cookies"]
        names = [name for name, _, _ in _iter_cookies(cookies)]
        hit = _has_marker(cookies, host, site.markers)
        _debug("%s取到 %d 条，命中登录标记=%s，样本=%s",
               prefix, len(cookies), hit, names[:8])
        if not hit:
            return False
        # 登录成功后站点通常会连续下发几条 Cookie（SESSDATA、bili_jct…），
        # 稍等一下再取一次，避免只拿到半套
        time.sleep(SAVE_SETTLE_SECONDS)
        again = _grab()
        if "cookies" in again:
            cookies = again["cookies"]
        header = _header_from_cookies(cookies, host)
        if not header:
            _debug("%s命中登录标记但请求头为空（域名不匹配）", prefix)
            return False
        ok = sessions.save(site.key, header)
        state["saved"] = ok
        _debug("%s保存登录状态：%s（%d 字符）→ %s", prefix,
               "成功" if ok else "失败", len(header), sessions.current_path())
        return ok

    def _watch() -> None:
        _debug("登录窗已打开：%s → %s", site.key, site.url)
        time.sleep(WARMUP_SECONDS)      # 等 WebView2 起来再开始取
        started_at = time.monotonic()
        while not state["done"]:
            if time.monotonic() - started_at > MAX_WAIT_SECONDS:
                _debug("等待登录超时（%d 秒），关闭窗口", MAX_WAIT_SECONDS)
                break
            if _try_save():
                break
            if state["final_check"]:
                _debug("用户手动关窗，完成最后一次检查")
                break
            time.sleep(POLL_SECONDS)
        state["done"] = True
        _debug("登录窗结束，saved=%s", state["saved"])
        try:
            window.destroy()
        except Exception:            # noqa: BLE001 用户可能已经自己关掉
            pass

    def _on_closing():
        """用户手动关窗时先别关：让后台线程最后取一次再退出。

        这样即使轮询期间某次调用超时，登录完直接关窗也能保存下来。
        """
        if state["saved"] or state["closing_once"]:
            return True
        state["closing_once"] = True
        state["final_check"] = True
        return False

    window.events.closing += _on_closing

    # 自己起线程而不用 webview.start(func)：打包版里那个回调没有执行
    # （实测窗口开了、轮询却没启动），普通线程在所有形态下都可靠。
    # 开头先等窗口把 WebView2 初始化完，避免第一次取 Cookie 落空。
    threading.Thread(target=_watch, daemon=True, name="login-watch").start()

    try:
        webview.start()
    except Exception as exc:         # noqa: BLE001 WebView2 环境问题
        raise DesktopUnavailable(f"登录窗口打开失败：{exc}") from exc

    return EXIT_OK if state["saved"] else EXIT_CANCELLED


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口：``DLY.exe --login bilibili``。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--login" not in args:
        return EXIT_CANCELLED
    index = args.index("--login")
    platform = args[index + 1] if index + 1 < len(args) else ""
    return run(platform)


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
