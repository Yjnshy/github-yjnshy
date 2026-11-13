"""桌面窗口：把界面装进独立窗口，双击即可用，不必打开浏览器。

为什么选 pywebview 而不是 Tkinter / PySide：

- Tkinter 依赖 tcl/tk，精简版 Python 常常不带（本项目实测环境就没有）
- PySide / PyQt 要带上百 MB 的 Qt 运行时，打包后的安装包会非常臃肿
- pywebview 直接复用系统自带的 WebView2（Win10/11 与新版 macOS 都有），
  界面代码零改动，将来做手机端时同一套前端可以直接复用

窗口关闭即视为退出：先停录制 / 值守任务，再关本地服务，不留孤儿进程。
"""

from __future__ import annotations

import logging
import threading
import time

from media_toolkit.gui.server import create_app

logger = logging.getLogger(__name__)

WINDOW_TITLE = "Media Toolkit — 视频下载 & 直播录制"
WINDOW_SIZE = (1180, 860)
MIN_SIZE = (960, 640)
QUIT_POLL_SECONDS = 0.3


class DesktopUnavailable(RuntimeError):
    """桌面窗口依赖缺失（没装 pywebview 或没有可用的 WebView2）。"""


def _screen_logical_size() -> tuple:
    """屏幕的「逻辑尺寸」：高 DPI（125% / 150%）下窗口按逻辑像素算。

    取不到时返回 (0, 0)，调用方回落到固定尺寸。
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hdc = user32.GetDC(0)
        dpi = gdi32.GetDeviceCaps(hdc, 88) or 96      # 88 = LOGPIXELSX
        user32.ReleaseDC(0, hdc)
        scale = (dpi or 96) / 96
        return (int(user32.GetSystemMetrics(0) / scale),
                int(user32.GetSystemMetrics(1) / scale))
    except Exception:  # noqa: BLE001 非 Windows 或受限环境
        return (0, 0)


def _window_size() -> tuple:
    """按屏幕大小收敛窗口尺寸，避免小屏或高分屏下底部被任务栏挡住。"""
    screen_w, screen_h = _screen_logical_size()
    if not screen_w or not screen_h:
        return WINDOW_SIZE
    width = max(MIN_SIZE[0], min(WINDOW_SIZE[0], int(screen_w * 0.9)))
    height = max(520, min(WINDOW_SIZE[1], int(screen_h * 0.82)))
    return (width, height)


def _load_webview():
    try:
        import webview  # noqa: F401  延迟导入：浏览器模式不需要它
    except ImportError as exc:
        raise DesktopUnavailable(
            "未安装桌面窗口组件 pywebview，请执行："
            "pip install \"media-toolkit[desktop]\""
        ) from exc
    return webview


def launch(debug: bool = False) -> None:
    """打开桌面窗口（阻塞，直到用户关窗或点「退出程序」）。"""
    webview = _load_webview()
    app = create_app(desktop=True)
    app.start()
    logger.debug("桌面窗口已启动：%s", app.url)

    width, height = _window_size()
    window = webview.create_window(
        WINDOW_TITLE, app.url,
        width=width, height=height,
        min_size=(min(MIN_SIZE[0], width), min(MIN_SIZE[1], height)),
        text_select=True,
    )

    def _quit_watcher() -> None:
        """界面里点「退出程序」时关掉窗口，让 GUI 主循环能返回。"""
        while not app.state.quit_requested:
            time.sleep(QUIT_POLL_SECONDS)
        time.sleep(0.2)
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 窗口可能已被用户先关掉
            logger.debug("销毁窗口失败", exc_info=True)

    threading.Thread(target=_quit_watcher, daemon=True,
                     name="quit-watcher").start()

    try:
        # debug=True 会打开 DevTools，方便排查界面问题
        webview.start(debug=debug)
    except Exception as exc:  # noqa: BLE001 WebView2 缺失等环境问题
        raise DesktopUnavailable(f"桌面窗口启动失败：{exc}") from exc
    finally:
        # 用户直接关窗时不会走 /api/quit，这里补上，保证任务被停止
        app.state.request_quit()
        app.shutdown()
        logger.info("桌面窗口已关闭")
