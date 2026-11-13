"""跨平台兼容层：把 Windows 专有写法收敛到一处。"""

from __future__ import annotations

import os
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"

# Windows 下弹黑窗抑制标志；其他平台为 0
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def popen_kwargs(isolate_console: bool = False) -> dict:
    """子进程通用参数：不弹窗、不从 stdin 读。

    ``isolate_console=True`` 会让子进程不接收终端的 Ctrl+C（Windows 下
    新建进程组即禁用 Ctrl+C 继承）。录制时开启它，让"何时停止 ffmpeg"
    的决定权留在我们手里：用户按 Ctrl+C 先由 Python 收到，再由
    :func:`kill_process_tree` 统一收尾，避免 ffmpeg 被信号直接打断。
    """
    kwargs: dict = {"stdin": subprocess.DEVNULL}
    if IS_WINDOWS:
        flags = CREATE_NO_WINDOW
        if isolate_console:
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = flags
    return kwargs


def kill_process_tree(proc: subprocess.Popen) -> None:
    """终止进程及其子进程（ffmpeg 需要整树杀掉）。"""
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        try:
            proc.kill()
        except OSError:
            pass


def open_folder(path: str) -> None:
    """在系统文件管理器中打开目录。"""
    if IS_WINDOWS:
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
