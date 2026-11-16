"""调系统原生的「选择文件夹」对话框，把结果交回前端。

为什么不用 Tkinter / pywebview 自带的对话框：

- Tkinter 依赖 tcl/tk，精简版 Python 常常不带（本项目实测环境就没有）
- pywebview 的 ``create_file_dialog`` 只能在它自己的 GUI 线程里安全调用，
  而界面请求走的是 HTTP 服务的工作线程，跨线程弹模态框容易把窗口卡死；
  更要紧的是 ``--web`` 浏览器模式下根本没有 pywebview，功能会直接失效

所以这里直接调 Windows 的 ``IFileOpenDialog``（就是资源管理器里那个「选择
文件夹」窗口，支持新建文件夹、地址栏、搜索）。走 ctypes 手工绑虚表，不引入
任何新依赖；浏览器模式与桌面模式共用同一套代码。

取目录是**阻塞**操作，会占住一个请求线程直到用户点确定或取消，这是有意的：
前端在 await 这个请求，拿到结果才能回填输入框。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

_HRESULT = ctypes.c_long

# ---- COM 常量 ----
CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2
COINIT_DISABLE_OLE1DDE = 0x4

CLSID_FILE_OPEN_DIALOG = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
IID_IFILE_OPEN_DIALOG = "{d57c7288-d4ad-4768-be02-9d969532d960}"
IID_ISHELL_ITEM = "{43826d1e-e718-42ee-bc55-a1e261c37bfe}"

# IFileOpenDialog::SetOptions 的选项位
FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040
FOS_PATHMUSTEXIST = 0x00000800
FOS_FILEMUSTEXIST = 0x00001000
FOS_NOCHANGEDIR = 0x00000008

# IShellItem::GetDisplayName 的 SIGDN_FILESYSPATH
SIGDN_FILESYSPATH = 0x80058000

_S_OK = 0


class _COMDLG_FILTERSPEC(ctypes.Structure):
    _fields_ = [("pszName", ctypes.c_wchar_p), ("pszSpec", ctypes.c_wchar_p)]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    value = _GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text),
                                        ctypes.byref(value))
    return value


def _method(ptr: int, index: int, *argtypes):
    """取出 COM 接口虚表里第 ``index`` 个方法，包成可调用对象。"""
    table = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(_HRESULT, ctypes.c_void_p, *argtypes)
    return proto(table[index])


def _release(ptr: int) -> None:
    if ptr:
        _method(ptr, 2)(ptr)


def _shell_item(path: str) -> int:
    """路径 → ``IShellItem`` 指针；失败返回 0。"""
    shell32 = ctypes.windll.shell32
    shell32.SHCreateItemFromParsingName.argtypes = [
        ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHCreateItemFromParsingName.restype = _HRESULT
    iid = _guid(IID_ISHELL_ITEM)
    item = ctypes.c_void_p()
    if shell32.SHCreateItemFromParsingName(
            ctypes.c_wchar_p(path), None, ctypes.byref(iid),
            ctypes.byref(item)) != _S_OK:
        return 0
    return item.value or 0


def _windows_pick_folder(initial: str, title: str) -> str:
    """Windows 原生选择文件夹；取消返回空串。"""
    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE)
    # S_OK(0)/S_FALSE(1) = 由本次调用初始化；RPC_E_CHANGED_MODE = 线程已初始化
    initialized = hr in (0, 1)

    dialog = ctypes.c_void_p()
    try:
        clsid = _guid(CLSID_FILE_OPEN_DIALOG)
        iid = _guid(IID_IFILE_OPEN_DIALOG)
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER,
                                  ctypes.byref(iid),
                                  ctypes.byref(dialog)) < 0:
            return ""
        ptr = dialog.value

        # IFileDialog::GetOptions / SetOptions —— 追加「选文件夹」等标志
        get_options = _method(ptr, 10, ctypes.POINTER(ctypes.c_uint32))
        options = ctypes.c_uint32(0)
        if get_options(ptr, ctypes.byref(options)) == _S_OK:
            options.value |= (FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM
                              | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR)
            _method(ptr, 9, ctypes.c_uint32)(ptr, options.value)

        if title:
            _method(ptr, 17, ctypes.c_wchar_p)(ptr, title)
        _method(ptr, 18, ctypes.c_wchar_p)(ptr, "选择此文件夹")

        # 起始位置：当前输入框里的目录，存在才设，否则交给系统默认
        valid_initial = bool(initial) and os.path.isdir(initial)
        if valid_initial:
            item = _shell_item(initial)
            if item:
                _method(ptr, 12, ctypes.c_void_p)(ptr, item)   # SetFolder
                _release(item)
            else:
                # 退路：构造不出 IShellItem 时让它直接导航过去
                _method(ptr, 15, ctypes.c_wchar_p)(ptr, initial)  # SetFileName

        # IModalWindow::Show —— 阻塞到用户确定/取消
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        result = _method(ptr, 3, wintypes.HWND)(ptr, hwnd)
        if result != _S_OK:
            return ""

        # IFileDialog::GetResult → IShellItem::GetDisplayName
        picked = ctypes.c_void_p()
        if _method(ptr, 20, ctypes.POINTER(ctypes.c_void_p))(
                ptr, ctypes.byref(picked)) != _S_OK or not picked.value:
            return ""
        try:
            buf = ctypes.c_void_p()
            if _method(picked.value, 5, ctypes.c_uint32,
                       ctypes.POINTER(ctypes.c_void_p))(
                    picked.value, SIGDN_FILESYSPATH, ctypes.byref(buf)) != _S_OK:
                return ""
            try:
                return ctypes.wstring_at(buf.value) if buf.value else ""
            finally:
                ole32.CoTaskMemFree(buf)
        finally:
            _release(picked.value)
    except (AttributeError, OSError):
        return ""
    finally:
        _release(dialog.value)
        if initialized:
            ole32.CoUninitialize()


def _windows_pick_file(initial: str, title: str) -> str:
    """Windows 原生「选择文件」对话框；取消返回空串。"""
    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE)
    initialized = hr in (0, 1)

    dialog = ctypes.c_void_p()
    try:
        clsid = _guid(CLSID_FILE_OPEN_DIALOG)
        iid = _guid(IID_IFILE_OPEN_DIALOG)
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER,
                                  ctypes.byref(iid),
                                  ctypes.byref(dialog)) < 0:
            return ""
        ptr = dialog.value

        get_options = _method(ptr, 10, ctypes.POINTER(ctypes.c_uint32))
        options = ctypes.c_uint32(0)
        if get_options(ptr, ctypes.byref(options)) == _S_OK:
            options.value |= (FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
                              | FOS_FILEMUSTEXIST | FOS_NOCHANGEDIR)
            _method(ptr, 9, ctypes.c_uint32)(ptr, options.value)

        if title:
            _method(ptr, 17, ctypes.c_wchar_p)(ptr, title)
        _method(ptr, 18, ctypes.c_wchar_p)(ptr, "选择文件")

        # 文件类型过滤：只显示 cookies.txt / *.txt，默认 *.txt
        specs = (_COMDLG_FILTERSPEC * 2)(
            _COMDLG_FILTERSPEC("cookies.txt", "*.txt"),
            _COMDLG_FILTERSPEC("所有文件", "*.*"))
        _method(ptr, 8, ctypes.c_uint32,
                ctypes.POINTER(_COMDLG_FILTERSPEC))(ptr, 2, specs)
        _method(ptr, 6, ctypes.c_uint32)(ptr, 0)   # SetFileTypeIndex

        if initial and os.path.isfile(initial):
            folder = os.path.dirname(os.path.abspath(initial))
            item = _shell_item(folder)
            if item:
                _method(ptr, 12, ctypes.c_void_p)(ptr, item)   # SetFolder
                _release(item)
        elif initial and os.path.isdir(initial):
            item = _shell_item(initial)
            if item:
                _method(ptr, 12, ctypes.c_void_p)(ptr, item)
                _release(item)

        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if _method(ptr, 3, wintypes.HWND)(ptr, hwnd) != _S_OK:
            return ""

        picked = ctypes.c_void_p()
        if _method(ptr, 20, ctypes.POINTER(ctypes.c_void_p))(
                ptr, ctypes.byref(picked)) != _S_OK or not picked.value:
            return ""
        try:
            buf = ctypes.c_void_p()
            if _method(picked.value, 5, ctypes.c_uint32,
                       ctypes.POINTER(ctypes.c_void_p))(
                    picked.value, SIGDN_FILESYSPATH, ctypes.byref(buf)) != _S_OK:
                return ""
            try:
                return ctypes.wstring_at(buf.value) if buf.value else ""
            finally:
                ole32.CoTaskMemFree(buf)
        finally:
            _release(picked.value)
    except (AttributeError, OSError):
        return ""
    finally:
        _release(dialog.value)
        if initialized:
            ole32.CoUninitialize()


def _darwin_pick_folder(initial: str, title: str) -> str:
    script = f'POSIX path of (choose folder with prompt "{title}")'
    try:
        out = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=None)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _linux_pick_folder(initial: str, title: str) -> str:
    cmd = ["zenity", "--file-selection", "--directory", f"--title={title}"]
    if initial and os.path.isdir(initial):
        cmd.append(f"--filename={initial.rstrip(os.sep)}{os.sep}")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def pick_folder(initial: str = "", title: str = "选择文件夹") -> str:
    """弹系统选择文件夹对话框，返回绝对路径；用户取消返回空串。

    ``initial`` 仅作为起始位置提示，不存在时忽略。
    """
    initial = os.path.expanduser(str(initial or "").strip())
    if IS_WINDOWS:
        return _windows_pick_folder(initial, title)
    if sys.platform == "darwin":
        return _darwin_pick_folder(initial, title)
    return _linux_pick_folder(initial, title)


def pick_file(initial: str = "", title: str = "选择文件") -> str:
    """弹系统选择文件对话框，返回绝对路径；用户取消返回空串。

    和 ``pick_folder`` 一样走 Windows 原生 IFileOpenDialog，浏览器模式也能用，
    不依赖 pywebview 的 GUI 线程。非 Windows 平台目前没有实现，返回空串。
    """
    if not IS_WINDOWS:
        return ""
    return _windows_pick_file(os.path.expanduser(str(initial or "").strip()), title)
