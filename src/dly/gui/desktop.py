"""桌面窗口：把界面装进独立窗口，双击即可用，不必打开浏览器。

为什么选 pywebview 而不是 Tkinter / PySide：

- Tkinter 依赖 tcl/tk，精简版 Python 常常不带（本项目实测环境就没有）
- PySide / PyQt 要带上百 MB 的 Qt 运行时，打包后的安装包会非常臃肿
- pywebview 直接复用系统自带的 WebView2（Win10/11 与新版 macOS 都有），
  界面代码零改动，将来做手机端时同一套前端可以直接复用

窗口关闭即视为退出：先停录制 / 值守任务，再关本地服务，不留孤儿进程。
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes

from dly.gui.server import create_app

logger = logging.getLogger(__name__)

WINDOW_TITLE = "帅羊羊DLY — 视频下载 & 直播录制"
WINDOW_SIZE = (1180, 860)
MIN_SIZE = (960, 640)
QUIT_POLL_SECONDS = 0.3


class DesktopUnavailable(RuntimeError):
    """桌面窗口依赖缺失（没装 pywebview 或没有可用的 WebView2）。"""


def alert(message: str, title: str = "帅羊羊DLY") -> None:
    """弹一个系统对话框。

    打包成窗口程序后没有控制台，启动失败时用户只会看到「什么都没发生」，
    必须用系统弹窗把原因讲清楚。
    """
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, str(message), title, 0x10)
    except Exception:  # noqa: BLE001 非 Windows 或受限环境，放弃弹窗
        logger.error("%s: %s", title, message)


def _dpi_scale() -> float:
    """系统缩放比例（125% → 1.25）；取不到按 1.0 处理。"""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hdc = user32.GetDC(0)
        dpi = gdi32.GetDeviceCaps(hdc, 88) or 96      # 88 = LOGPIXELSX
        user32.ReleaseDC(0, hdc)
        return (dpi or 96) / 96
    except Exception:  # noqa: BLE001 非 Windows 或受限环境
        return 1.0


def _work_area() -> tuple:
    """桌面上真正能放窗口的区域（已排除任务栏），单位物理像素。

    比 GetSystemMetrics 靠谱：任务栏、Dock 之类占掉的高度不会再被算进去，
    否则窗口底部正好被任务栏切掉——这是高分屏上最常见的排版事故。
    """
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:  # noqa: BLE001
        pass
    return (0, 0, 0, 0)


def _window_geometry() -> tuple:
    """算出窗口尺寸与位置（逻辑像素），保证整个窗口落在可用区域内。

    pywebview 的 width/height/x/y 都按逻辑像素处理（会再乘上 DPI 缩放），
    所以这里统一先把物理像素的可用区域换算成逻辑像素再收敛。
    """
    left, top, right, bottom = _work_area()
    if right <= left or bottom <= top:
        return WINDOW_SIZE, (None, None)

    scale = _dpi_scale() or 1.0
    area_w = int((right - left) / scale)
    area_h = int((bottom - top) / scale)
    margin = 20                                    # 四周留一点呼吸空间

    width = min(WINDOW_SIZE[0], max(640, area_w - margin))
    height = min(WINDOW_SIZE[1], max(480, area_h - margin))
    x = int(left / scale) + max(0, (area_w - width) // 2)
    y = int(top / scale) + max(0, (area_h - height) // 2)
    return (width, height), (x, y)


def _window_size() -> tuple:
    """只取尺寸（测试与兼容用）。"""
    return _window_geometry()[0]


def _load_webview():
    # 先备好 .NET 运行时：打包版走 coreclr + 打过补丁的 winforms 后端
    from dly.gui import clr_boot

    clr_boot.prepare()
    try:
        import webview  # noqa: F401  延迟导入：浏览器模式不需要它
    except ImportError as exc:
        raise DesktopUnavailable(
            "未安装桌面窗口组件 pywebview，请执行："
            "pip install \"dly[desktop]\""
        ) from exc
    return webview





def _ensure_minimize_style(window) -> None:
    """无边框窗口默认没有 WS_MINIMIZEBOX，任务栏点了只会聚焦、不会最小化。

    补上最小化 / 最大化样式位，恢复任务栏图标的正常开关行为。
    """
    user32 = ctypes.windll.user32
    # 不要信 window.native.Handle：它可能是 WebView2 子窗口，改了也没用。
    # 按标题找顶层窗口最稳。
    ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    found = []

    def cb(h, _):
        n = user32.GetWindowTextLengthW(h)
        if n and user32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            if "帅羊羊DLY" in buf.value:
                found.append(h)
        return True

    user32.EnumWindows(ENUM(cb), 0)
    hwnd = found[0] if found else 0
    if not hwnd:
        # 某些会话里 EnumWindows 拿不到自己的窗口，直接按标题找
        for title in (WINDOW_TITLE, "帅羊羊DLY", "DLY"):
            candidate = user32.FindWindowW(None, title)
            if candidate:
                hwnd = candidate
                break
    if not hwnd:
        return
    style = user32.GetWindowLongPtrW(hwnd, -16)      # GWL_STYLE
    wanted = 0x20000 | 0x10000                       # MINIMIZEBOX | MAXIMIZEBOX
    if not (style & wanted):
        user32.SetWindowLongPtrW(hwnd, -16, style | wanted)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            0x0001 | 0x0002 | 0x0040)  # NOMOVE|NOSIZE|FRAMECHANGED


def _focus_existing_instance() -> bool:
    """已有 DLY 窗口时切到前台并退出本次启动，避免重复实例。"""
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    found = []

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if not n or not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if "帅羊羊DLY" not in buf.value:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != os.getpid():
            found.append(hwnd)
        return True

    user32.EnumWindows(ENUM(cb), 0)
    for hwnd in found:
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
    return bool(found)


def launch(debug: bool = False) -> None:
    """打开桌面窗口（阻塞，直到用户关窗或收到 /api/quit 退出请求）。"""
    webview = _load_webview()
    if _focus_existing_instance():
        logger.info("已有 DLY 窗口，切到前台后退出本次启动")
        return
    app = create_app(desktop=True)
    app.start()
    logger.debug("桌面窗口已启动：%s", app.url)

    (width, height), (x, y) = _window_geometry()
    window = webview.create_window(
        WINDOW_TITLE, app.url,
        width=width, height=height,
        x=x, y=y,
        min_size=(min(MIN_SIZE[0], width), min(MIN_SIZE[1], height)),
        text_select=True,
        # 去掉系统标题栏：界面里那一条自绘标题栏才是真正的最顶栏。
        # easy_drag 让标题栏空白处也能拖动窗口（输入框 / 按钮不受影响）。
        frameless=True,
        easy_drag=True,
        shadow=True,
    )

    # 自绘标题栏的最小化 / 最大化 / 关闭由本地服务转发到窗口对象
    maximized = {"value": False}

    def _toggle_maximize() -> None:
        if maximized["value"]:
            window.restore()
        else:
            window.maximize()
        maximized["value"] = not maximized["value"]

    app.state.window_actions = {
        "minimize": window.minimize,
        "maximize": _toggle_maximize,
        "close": window.destroy,
    }

    def _quit_watcher() -> None:
        """收到退出请求时关掉窗口，让 GUI 主循环能返回。"""
        while not app.state.quit_requested:
            time.sleep(QUIT_POLL_SECONDS)
        time.sleep(0.2)
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 窗口可能已被用户先关掉
            logger.debug("销毁窗口失败", exc_info=True)

    threading.Thread(target=_quit_watcher, daemon=True,
                     name="quit-watcher").start()

    def _apply_style() -> None:
        if os.name == "nt":
            try:
                _ensure_minimize_style(window)
            except Exception:  # noqa: BLE001
                logger.debug("补窗口样式失败", exc_info=True)

    if os.name == "nt":
        def _style_loop() -> None:
            while True:
                time.sleep(1)
                _apply_style()

        threading.Thread(target=_style_loop, daemon=True,
                         name="window-style-loop").start()

    try:
        webview.start(debug=debug, func=_apply_style)
    except Exception as exc:  # noqa: BLE001 WebView2 缺失等环境问题
        raise DesktopUnavailable(f"桌面窗口启动失败：{exc}") from exc
    finally:
        # 用户直接关窗时不会走 /api/quit，这里补上，保证任务被停止
        app.state.request_quit()
        app.shutdown()
        logger.info("桌面窗口已关闭")
