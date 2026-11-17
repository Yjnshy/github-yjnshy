"""启动自检：把「这个进程到底能不能写」记录下来。

为什么要这个东西：打包版出现过同一份代码在有的机器上写不进输出目录
（``Permission denied``），而同一个目录用普通 python 进程写却正常。
窗口程序没有控制台，弹出框也会被用户关掉，唯一可靠的办法是让它自己
把结论写进文件。

只在打包运行时调用，输出 ``<程序目录>/write_diag.txt``。
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

NAME = "write_diag.txt"


def _integrity_level() -> str:
    """当前进程的完整性级别（有沙箱/受限令牌时能看出来）。"""
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.windll.advapi32
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
                ctypes.windll.kernel32.GetCurrentProcess(), 0x0008,
                ctypes.byref(token)):
            return "未知（OpenProcessToken 失败）"
        try:
            size = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, 25, buf, size.value,
                                                ctypes.byref(size)):
                return "未知（GetTokenInformation 失败）"
            # TOKEN_MANDATORY_LABEL 里的 SID 最后一个子授权就是 RID
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            count = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))[1]
            last = ctypes.cast(sid, ctypes.POINTER(ctypes.c_uint))[count + 1]
            return {0x0000: "Untrusted", 0x1000: "低 Low",
                    0x2000: "中 Medium", 0x3000: "高 High",
                    0x4000: "System"}.get(last & 0xF000, hex(last))
        finally:
            ctypes.windll.kernel32.CloseHandle(token)
    except Exception as exc:            # noqa: BLE001 非 Windows 或受限环境
        return f"未知（{exc}）"


def _try_write(directory: Path) -> str:
    """真实建一个文件，返回可读结论。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".dly_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return "可写"
    except OSError as exc:
        return f"不可写：{exc!r}"


def dump(extra_dirs: List[Path] | None = None) -> Path | None:
    """写出自检结果；返回文件路径（失败时返回 None）。"""
    if not getattr(sys, "frozen", False):
        return None
    try:
        target = Path(sys.executable).resolve().parent / NAME
    except OSError:
        return None

    from dly.core import paths

    dirs = [paths.data_dir(), paths.log_dir()]
    if extra_dirs:
        dirs.extend(extra_dirs)

    lines = [
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"程序：{sys.executable}",
        f"用户：{os.environ.get('USERNAME')} / 完整性级别：{_integrity_level()}",
        f"工作目录：{os.getcwd()}",
        f"家目录：{Path.home()}",
        "",
    ]
    for d in dirs:
        lines.append(f"{d} -> {_try_write(Path(d))}")

    try:
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target
    except OSError as exc:
        logger.debug("写自检文件失败: %s", exc)
        return None
