"""图形界面包：桌面窗口（pywebview）+ 内置 HTTP 服务 + 单页前端。

入口约定：

- ``dly gui`` / ``python -m dly.gui`` → 优先独立桌面窗口
- 缺少 pywebview 时自动退回系统浏览器，不影响使用
- 打包成 exe 后没有控制台：桌面窗口起不来（缺 WebView2 / .NET 运行时）也
  退回浏览器模式继续用，只弹窗说明原因；空闲自退负责关掉页面后收尾进程
"""

from __future__ import annotations

from dly.gui.desktop import DesktopUnavailable, alert
from dly.gui.server import launch as launch_web

__all__ = ["launch", "launch_web", "DesktopUnavailable"]


def launch(debug: bool = False, web: bool = False) -> None:
    """启动界面：默认独立桌面窗口，``web=True`` 或缺少依赖时用浏览器。"""
    if web:
        launch_web()
        return
    try:
        from dly.gui.desktop import launch as launch_desktop

        launch_desktop(debug=debug)
    except DesktopUnavailable as exc:
        from dly.core import paths

        if paths.is_frozen():
            # 打包版没有控制台，直接退出用户只能看到一个弹窗。HTTP 服务是
            # 纯 Python，不依赖 WebView2 / .NET，退回浏览器模式照样能用；
            # 空闲自退保证用户关掉页面后进程不会变成后台僵尸。
            alert(
                f"{exc}\n\n"
                "程序将改用浏览器模式继续运行：稍后会自动打开默认浏览器，"
                "功能与桌面窗口完全相同；关闭浏览器页面且没有任务在跑时，"
                "程序会自动退出。\n\n"
                "想用回独立桌面窗口，请在这台电脑安装下面两个微软官方运行时，"
                "装好后重新打开 DLY.exe：\n"
                "1. Microsoft Edge WebView2 运行时\n"
                "2. Microsoft .NET Framework 4.8 运行时\n"
                "（浏览器打开时可能会被拦截，允许即可）"
            )
            launch_web(open_browser=True, idle_exit_seconds=150)
            return
        print(f"{exc}\n已改用系统浏览器打开界面。")
        launch_web()
