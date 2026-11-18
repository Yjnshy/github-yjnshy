"""一次性子进程：给无边框主窗口补上最小化 / 最大化样式位。

为什么单独起进程：实测由主程序自己的线程调用 SetWindowLongPtr 改 GWL_STYLE
不生效，而独立进程调用一次就有效。用法是 DLY.exe --patch-window-style。
"""

from __future__ import annotations

import sys
import time


def _patch() -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    wanted = 0x20000 | 0x10000                  # WS_MINIMIZEBOX | WS_MAXIMIZEBOX

    for _ in range(40):                          # 最多等 20 秒，主窗口可能还没出现
        found = []

        def cb(h, _arg):
            n = user32.GetWindowTextLengthW(h)
            if n and user32.IsWindowVisible(h):
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(h, buf, n + 1)
                if "帅羊羊DLY" in buf.value:
                    found.append(h)
            return True

        user32.EnumWindows(ENUM(cb), 0)
        done = False
        for h in found:
            style = user32.GetWindowLongPtrW(h, -16)
            if (style & wanted) != wanted:
                user32.SetWindowLongPtrW(h, -16, style | wanted)
                user32.SetWindowPos(h, 0, 0, 0, 0, 0,
                                    0x0001 | 0x0002 | 0x0040)
            done = True
        if done:
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    if sys.platform != "win32":
        return 0
    try:
        _patch()
    except Exception:  # noqa: BLE001 助手失败不该影响主程序
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
