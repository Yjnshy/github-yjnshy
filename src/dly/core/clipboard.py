"""系统剪贴板读写（UTF-16 文本）。

桌面窗口用 pywebview + WebView2，而 pywebview 6.x 在 Windows 上把
``AreDefaultContextMenusEnabled`` 绑死在 debug 开关上 —— 发布版没有原生右键菜单，
输入框里自然也就没有「剪切 / 复制 / 粘贴」。界面因此自己画一个右键菜单
（见 ``index.html``），通过本地 HTTP 接口调这里的函数读写系统剪贴板。

为什么不直接用 ``navigator.clipboard``：WebView2 里读剪贴板需要用户手势 + 权限，
不同版本行为不一致；而服务本来就跑在本机，走 Win32 API 最稳、也无需弹权限框。
非 Windows 平台返回 ``None`` / ``False``，前端会退回浏览器的 Clipboard API。

返回值约定：``read_text()`` 返回 ``None`` 表示「读不到/不支持」，返回 ``""``
表示剪贴板里确实没有文本 —— 前端靠这个区分要不要走回退路径。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
_OPEN_RETRIES = 6          # 剪贴板常被其它程序短暂占用
_OPEN_RETRY_DELAY = 0.02   # 秒


def _api():
    """按需绑定 Win32 函数签名，避免在导入时就碰 ctypes.windll。

    指针一律用 ``c_void_p``：不显式指定 restype 时 ctypes 默认按 c_int 返回，
    在 64 位进程里会把剪贴板句柄截断，这是最容易踩的坑。
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    return user32, kernel32


def _open_clipboard(user32) -> bool:
    for _ in range(_OPEN_RETRIES):
        if user32.OpenClipboard(None):
            return True
        time.sleep(_OPEN_RETRY_DELAY)
    return False


def _windows_read() -> str:
    user32, kernel32 = _api()
    if not _open_clipboard(user32):
        raise OSError("剪贴板被其它程序占用")
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            # CF_UNICODETEXT 是 UTF-16LE，wstring_at 遇到结尾的 NUL 自动停
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _windows_write(text: str) -> bool:
    user32, kernel32 = _api()
    data = text.encode("utf-16-le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)

    if not _open_clipboard(user32):
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        # SetClipboardData 成功后句柄归系统所有，绝不能再 GlobalFree
        return True
    finally:
        user32.CloseClipboard()


def read_text() -> str | None:
    """读取剪贴板文本；失败或平台不支持返回 ``None``。"""
    if not IS_WINDOWS:
        return None
    try:
        return _windows_read()
    except Exception:  # noqa: BLE001 读不到剪贴板不该拖垮界面请求
        logger.debug("读取剪贴板失败", exc_info=True)
        return None


def write_text(text: str) -> bool:
    """把文本写入剪贴板，返回是否成功。"""
    if not IS_WINDOWS:
        return False
    try:
        return _windows_write(text)
    except Exception:  # noqa: BLE001
        logger.debug("写入剪贴板失败", exc_info=True)
        return False
