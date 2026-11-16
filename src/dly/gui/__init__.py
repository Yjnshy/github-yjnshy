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
            # 打包版没有控制台。优先帮用户把缺的系统运行时装上（WebView2 /
            # .NET 4.8）；装不了或用户拒绝，再退回纯 Python 的浏览器模式。
            _try_install_runtimes(str(exc))
            alert(
                f"{exc}\n\n"
                "程序将改用浏览器模式继续运行：稍后会自动打开默认浏览器，"
                "功能与桌面窗口完全相同；关闭浏览器页面且没有任务在跑时，"
                "程序会自动退出。\n\n"
                "想用回独立桌面窗口，可以重新打开 DLY.exe 让它再检测一次，"
                "或手动安装：Microsoft Edge WebView2 运行时 + "
                "Microsoft .NET Framework 4.8。\n"
                "（若刚装完运行时仍失败，请重启电脑后再试）"
            )
            launch_web(open_browser=True, idle_exit_seconds=150)
            return
        print(f"{exc}\n已改用系统浏览器打开界面。")
        launch_web()


def _try_install_runtimes(exc_text: str) -> None:
    """检测缺失的桌面运行时并自动安装；成功则换新进程重启。

    pythonnet 初始化失败的状态会留在当前进程里，所以装完必须重新拉起
    DLY.exe，不能直接重试 launch_desktop()。
    """
    try:
        from dly.gui import runtime_setup

        missing = runtime_setup.missing_components()
        repair = not missing and runtime_setup.looks_like_runtime_failure(exc_text)
        targets = runtime_setup.repair_targets() if repair else missing
        if not targets:
            return              # 两样都在、错误也和环境无关，装了也没用
        if not runtime_setup.confirm_install(targets, repair=repair):
            return
        if runtime_setup.install(targets):
            runtime_setup.relaunch()
            raise SystemExit(0)
    except SystemExit:
        raise
    except Exception:           # noqa: BLE001 自动安装只是锦上添花，失败就走浏览器
        import logging

        logging.getLogger("dly").warning("自动安装运行环境失败", exc_info=True)
