"""图形界面包：桌面窗口（pywebview）+ 内置 HTTP 服务 + 单页前端。

入口约定：

- ``dly gui`` / ``python -m dly.gui`` → 优先独立桌面窗口
- 缺少 pywebview 时自动退回系统浏览器，不影响使用
- 打包成 exe 后没有控制台：桌面窗口起不来时静默退回浏览器模式继续用；
  只有确实缺运行时需要用户决策时才弹窗，空闲自退负责关掉页面后收尾进程
"""

from __future__ import annotations

import logging

from dly.core import paths
from dly.gui.desktop import DesktopUnavailable, alert
from dly.gui.server import launch as launch_web

logger = logging.getLogger("dly")

__all__ = ["launch", "launch_web", "DesktopUnavailable"]


def launch(debug: bool = False, web: bool = False) -> None:
    """启动界面：默认独立桌面窗口，``web=True`` 时用浏览器。"""
    if web:
        launch_web()
        return
    try:
        from dly.gui.desktop import launch as launch_desktop

        launch_desktop(debug=debug)
        return
    except DesktopUnavailable as exc:
        if not paths.is_frozen():
            print(f"{exc}\n已改用系统浏览器打开界面。")
            launch_web()
            return
        # 打包版没有控制台：桌面窗口起不来时先看是不是缺系统运行时，
        # 只在这种确实能装点什么的情况下才弹窗打扰用户。
        from dly.gui import runtime_setup

        need_prompt = bool(runtime_setup.missing_components())
        _try_install_runtimes(str(exc))
        if need_prompt:
            alert(
                f"{exc}\n\n"
                "程序将改用浏览器模式继续运行：稍后会自动打开默认浏览器，"
                "功能与桌面窗口完全相同；关闭浏览器页面且没有任务在跑时，"
                "程序会自动退出。\n\n"
                "想用回独立桌面窗口，可重新打开 DLY.exe 让它再检测一次；"
                "若刚才选了「打开官方页面」，按页面引导装好后重新打开程序即可。"
                "（装完运行时仍失败时，重启电脑再试）"
            )
        logger.info("桌面窗口不可用，改用浏览器模式：%s", exc)
        launch_web(open_browser=True, idle_exit_seconds=150)


def _try_install_runtimes(exc_text: str) -> None:
    """只有注册表确实显示缺组件时才问安装；显示齐全就不再追问。

    v0.1.4 曾把 pythonnet 加载失败一律当成"需要修复"，导致电脑上两样
    都装了还反复弹安装窗。现在缺才问，不缺就安静退回浏览器模式。
    """
    try:
        from dly.gui import runtime_setup

        missing = runtime_setup.missing_components()
        if not missing:
            return              # 系统显示齐全：安装帮不上忙，别再烦用户
        action = runtime_setup.prompt_runtime_action(missing)
        if action == "pages":
            runtime_setup.open_download_pages(missing)
            return
        if action == "skip":
            return
        if runtime_setup.install(missing):
            runtime_setup.relaunch()
            raise SystemExit(0)
    except SystemExit:
        raise
    except Exception:           # noqa: BLE001 自动安装只是锦上添花，失败就走浏览器
        import logging

        logging.getLogger("dly").warning("自动安装运行环境失败", exc_info=True)
