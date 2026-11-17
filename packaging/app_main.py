"""PyInstaller 打包后的程序入口。

为什么单独一个入口文件，而不是直接指向 ``dly/gui/__main__.py``：

- 打包后的窗口程序**没有控制台**，任何未捕获异常都会让程序「闪一下就没了」，
  用户完全不知道发生了什么。这里统一兜底：写日志 + 弹系统提示框
- ``multiprocessing.freeze_support()`` 必须在最早期调用，否则打包后一旦用到
  多进程就会无限自我复制（Windows 上没有 fork，子进程要重新执行入口）
"""

from __future__ import annotations

import logging
import multiprocessing
import sys


def _show_error(message: str) -> None:
    """弹窗报错；弹不出来就退回日志。"""
    try:
        from dly.gui.desktop import alert

        alert(message)
    except Exception:  # noqa: BLE001 连 GUI 模块都导入不了时只能记日志
        logging.getLogger("dly").error(message)


def main() -> int:
    # Windows 多进程支持：必须在导入重量级模块之前调用
    multiprocessing.freeze_support()

    from dly.core.logging import setup_logging

    log_dir = setup_logging()

    # 打包版先自检一次“能不能写”：出问题时用户只需把这个文件发过来
    try:
        from pathlib import Path

        from dly.core import diag
        from dly.core.config import load_config

        diag.dump([Path(load_config().output_dir)])
    except Exception:                             # noqa: BLE001 自检失败不影响启动
        logging.getLogger("dly").debug("写权限自检失败", exc_info=True)

    # --login <平台>：只开登录窗然后退出。主程序用同样的 exe 再拉起一个自己，
    # 因为 webview.start() 必须独占主线程，不能和主窗口挤在同一个进程里。
    if "--login" in sys.argv:
        from dly.gui import login_window

        return login_window.main()

    try:
        from dly.gui import launch

        launch(debug="--debug" in sys.argv, web="--web" in sys.argv)
        return 0
    except SystemExit as exc:                     # 用户主动退出
        return int(exc.code or 0)
    except BaseException as exc:                  # noqa: BLE001 兜底，避免闪退
        logging.getLogger("dly").exception("程序异常退出")
        _show_error(
            f"程序启动失败：{exc}\n\n"
            f"日志文件：{log_dir / 'dly.log'}\n"
            f"若反复出现，请把该日志一并反馈。"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
