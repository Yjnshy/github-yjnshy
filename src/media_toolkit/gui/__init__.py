"""图形界面包：桌面窗口（pywebview）+ 内置 HTTP 服务 + 单页前端。

入口约定：

- ``mtk gui`` / ``python -m media_toolkit.gui`` → 优先独立桌面窗口
- 缺少 pywebview 时自动退回系统浏览器，不影响使用
- 打包成 exe 后没有控制台，退回浏览器毫无意义，改为弹窗说明原因
"""

from __future__ import annotations

from media_toolkit.gui.desktop import DesktopUnavailable, alert
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
        from media_toolkit.core import paths

        if paths.is_frozen():
            # 打包后的程序没有控制台，print 出去用户根本看不到
            alert(f"{exc}\n\n请安装 Microsoft Edge WebView2 运行时后重试。")
            raise SystemExit(1) from exc
        print(f"{exc}\n已改用系统浏览器打开界面。")
        launch_web()
