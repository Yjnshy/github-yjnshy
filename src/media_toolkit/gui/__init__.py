"""图形界面包：桌面窗口（pywebview）+ 内置 HTTP 服务 + 单页前端。

入口约定：

- ``mtk gui`` / ``python -m media_toolkit.gui`` → 优先独立桌面窗口
- 缺少 pywebview 时自动退回系统浏览器，不影响使用
"""

from __future__ import annotations

from media_toolkit.gui.desktop import DesktopUnavailable
from media_toolkit.gui.server import launch as launch_web

__all__ = ["launch", "launch_web", "DesktopUnavailable"]


def launch(debug: bool = False, web: bool = False) -> None:
    """启动界面：默认独立桌面窗口，``web=True`` 或缺少依赖时用浏览器。"""
    if web:
        launch_web()
        return
    try:
        from media_toolkit.gui.desktop import launch as launch_desktop

        launch_desktop(debug=debug)
    except DesktopUnavailable as exc:
        print(f"{exc}\n已改用系统浏览器打开界面。")
        launch_web()
