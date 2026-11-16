"""在桌面创建程序快捷方式。

实现方式：直接调用 Windows 的 ``IShellLink`` COM 接口（通过 ctypes），
由系统自己写出 ``.lnk``，因此生成的快捷方式一定能被资源管理器执行。

> 早前试过手工按 MS-SHLLINK 规范拼二进制：文件结构合法、也能被解析出
> 正确的目标路径，但 ShellExecute 会返回 31 (SE_ERR_NOASSOC) —— 缺少
> Shell 执行所需的内部标识。所以这里走系统接口。

用法::

    python packaging/make_shortcut.py                        # 建到当前用户桌面
    python packaging/make_shortcut.py --name "帅羊羊DLY"      # 自定义名称
    python packaging/make_shortcut.py --app-dir "D:/Apps/DLY"
    python packaging/make_shortcut.py --icon "D:/Apps/DLY/DLY_v2.ico"  # 只换图标
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import shutil
import sys
from ctypes import wintypes
from pathlib import Path

# ---- COM 常量 ----
CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
IID_ISHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
IID_IPERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 2

# IShellLinkW 的虚表下标（IUnknown 占 0-2）
_VT_RELEASE = 2
_VT_SET_DESCRIPTION = 7
_VT_SET_WORKING_DIR = 9
_VT_SET_ARGUMENTS = 11
_VT_SET_ICON_LOCATION = 17
_VT_SET_PATH = 20
# IPersistFile
_VT_QUERY_INTERFACE = 0
_VT_SAVE = 6

_HRESULT = ctypes.c_long


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    value = _GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(value))
    return value


def _vt_call(ptr: int, index: int, *argtypes):
    """取出 COM 接口某一下标的方法，包装成可调用对象。"""
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(_HRESULT, ctypes.c_void_p, *argtypes)
    return proto(vtable[index])


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} 失败，HRESULT=0x{hr & 0xFFFFFFFF:08X}")


def desktop_dir() -> Path:
    """当前用户桌面（兼容 OneDrive 重定向）。"""
    try:
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        # CSIDL_DESKTOPDIRECTORY = 0x10, SHGFP_TYPE_CURRENT = 0
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:
            return Path(buf.value)
    except Exception:  # noqa: BLE001 非 Windows 或 API 受限
        pass
    return Path(os.path.expanduser("~")) / "Desktop"


def find_exe(app_dir: Path | None = None) -> Path:
    """定位要指向的 exe。"""
    root = Path(__file__).resolve().parent.parent
    candidates = [Path(app_dir)] if app_dir else []
    candidates += [root / "dist" / "DLY", root]
    for folder in candidates:
        exe = folder / "DLY.exe"
        if exe.is_file():
            return exe
    raise FileNotFoundError(
        "没找到 DLY.exe。请先运行 scripts\\打包exe.bat，"
        "或用 --app-dir 指定程序所在目录。")


def versioned_icon(exe: Path) -> Path:
    """按图标内容生成带哈希的独立 .ico，绕开 Windows 图标缓存。

    资源管理器按「图标路径」缓存图标：exe 重打包或换了图标，只要快捷方式
    仍指向 ``DLY.ico`` 这个老路径，就会翻出缓存里的旧图。把内容哈希写进
    文件名后，图标内容一变路径就变，必然重新读取；内容不变则复用同一文件，
    不会每次运行都堆一个新 .ico。
    """
    src = Path(exe).parent / "DLY.ico"
    if not src.is_file():
        return Path(exe)          # 没有独立图标时退回 exe 内嵌图标
    try:
        digest = hashlib.sha256(src.read_bytes()).hexdigest()[:8]
        dst = src.parent / f"DLY_{digest}.ico"
        if not dst.is_file():
            shutil.copyfile(src, dst)
        return dst
    except OSError:
        return src                # 目录只读等情况下仍可用，只是缓存不刷新


def icon_source(exe: Path, override: str = "") -> Path:
    """快捷方式图标来源。

    默认用与 exe 同目录、按内容哈希命名的独立 .ico（见 ``versioned_icon``）。
    ``override`` 给「只换图标、还没重新打包 exe」时用，直接指定新路径即可
    跳过旧图标缓存，不用重启资源管理器。
    """
    if override:
        return Path(override).resolve()
    return versioned_icon(exe)


LAUNCHER_NAME = "启动DLY.lnk"


def _write_link(lnk: Path, target: Path, workdir: Path, arguments: str = "",
                icon: Path | None = None, description: str = "") -> Path:
    """用 IShellLink 写出一个 .lnk（桌面快捷方式与启动器共用）。"""
    lnk = Path(lnk)
    if lnk.exists():
        lnk.unlink()

    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    # S_OK(0) 与 S_FALSE(1) 都表示可用；RPC_E_CHANGED_MODE 说明已有别的模型，可继续
    initialized = hr in (0, 1)

    link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    try:
        clsid, iid_link = _guid(CLSID_SHELL_LINK), _guid(IID_ISHELL_LINK_W)
        iid_persist = _guid(IID_IPERSIST_FILE)
        _check(ole32.CoCreateInstance(
            ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(iid_link), ctypes.byref(link)), "创建 ShellLink 对象")

        ptr = link.value
        _vt_call(ptr, _VT_SET_PATH, ctypes.c_wchar_p, ctypes.c_int)(
            ptr, str(target), 0)
        _vt_call(ptr, _VT_SET_WORKING_DIR, ctypes.c_wchar_p)(ptr, str(workdir))
        if arguments:
            _vt_call(ptr, _VT_SET_ARGUMENTS, ctypes.c_wchar_p)(ptr, arguments)
        if icon:
            # SetIconLocation(路径, 索引)：路径只放 .ico 本身，索引单独传。
            # 早前把 ",0" 拼进路径字符串里，存下的路径本身非法，图标缓存一旦
            # 记住就长期显示空白。这里按规范分开传。
            _vt_call(ptr, _VT_SET_ICON_LOCATION, ctypes.c_wchar_p, ctypes.c_int)(
                ptr, str(icon), 0)
        if description:
            _vt_call(ptr, _VT_SET_DESCRIPTION, ctypes.c_wchar_p)(ptr, description)

        _check(_vt_call(ptr, _VT_QUERY_INTERFACE, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p))(
            ptr, ctypes.byref(iid_persist), ctypes.byref(persist)),
            "获取 IPersistFile")

        _check(_vt_call(persist.value, _VT_SAVE, ctypes.c_wchar_p, ctypes.c_int)(
            persist.value, str(lnk), 1), "写入 .lnk 文件")
        return lnk
    finally:
        for obj in (persist, link):
            if obj.value:
                _vt_call(obj.value, _VT_RELEASE)(obj.value)
        if initialized:
            ole32.CoUninitialize()


def launcher_link(exe: Path, icon: Path | None = None) -> Path:
    """在 exe 同目录生成「启动DLY.lnk」启动器并返回路径。

    为什么是快捷方式而不是 .cmd：
    * .cmd 的图标被系统固定成黑色命令窗口，改不了；快捷方式能带自己的
      IconLocation，和程序用同一只羊。
    * 未签名的 exe 被资源管理器直接双击会弹 SmartScreen「无法识别的应用」，
      而经命令解释器再 start 拉起不会。这里把它藏进快捷方式里。

    不用 VBS：本机 WSH 开了 UseWINSAFER，wscript 拉起后会挂住，什么都不发生。
    """
    exe = Path(exe).resolve()
    cmd_exe = (Path(os.environ.get("SystemRoot", r"C:\Windows"))
               / "System32" / "cmd.exe")
    return _write_link(
        exe.parent / LAUNCHER_NAME, cmd_exe, exe.parent,
        arguments=f'/c start "" "{exe}"',
        icon=icon or (exe.parent / "DLY.ico"),
        description="启动 DLY（跳过未签名提示）")


def create_shortcut(exe: Path, name: str = "帅羊羊DLY",
                    description: str = "", icon: str = "",
                    arguments: str = "", launcher: bool = False) -> Path:
    """在桌面创建程序快捷方式，返回 .lnk 路径。

    ``launcher=True`` 时不直接指向 DLY.exe，而是指向同目录的「启动DLY.lnk」：
    未签名的 exe 由资源管理器直接执行会触发 SmartScreen 的「无法识别的
    应用」（本机每次双击都弹），而经命令解释器 start 拉起则不会。
    """
    exe = Path(exe).resolve()
    if not exe.is_file():
        raise FileNotFoundError(f"目标不存在：{exe}")

    icon_path = icon_source(exe, icon)
    if launcher:
        # 指向同目录的启动器快捷方式：它自己带羊图标，双击时经 cmd 拉起
        target = launcher_link(exe, icon_path)
        workdir = exe.parent
        arguments = ""
    else:
        target = exe
        workdir = exe.parent

    return _write_link(desktop_dir() / f"{name}.lnk", target, workdir,
                       arguments, icon_path, description)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在桌面创建程序快捷方式")
    parser.add_argument("--app-dir", default="", help="程序所在目录（含 DLY.exe）")
    parser.add_argument("--name", default="帅羊羊DLY", help="快捷方式名称")
    parser.add_argument("--desc", default="视频无水印下载 & 直播录制", help="备注")
    parser.add_argument("--icon", default="",
                        help="指定 .ico 路径（换图标但还没重新打包 exe 时用）")
    parser.add_argument("--args", default="",
                        help="启动参数，例如 --args=--web 让快捷方式直接进浏览器模式")
    parser.add_argument("--launcher", action="store_true",
                        help="经同目录启动器（启动DLY.lnk）启动，避开未签名 exe 的 SmartScreen 提示")
    args = parser.parse_args(argv)

    try:
        exe = find_exe(Path(args.app_dir) if args.app_dir else None)
        lnk = create_shortcut(exe, args.name, args.desc, args.icon, args.args,
                              args.launcher)
    except (FileNotFoundError, OSError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    print(f"✓ 快捷方式：{lnk}")
    print(f"  指向：{exe}")
    print(f"  图标：{icon_source(exe, args.icon)}")
    if args.args:
        print(f"  参数：{args.args}")
    if args.launcher:
        print(f"  启动器：{exe.parent / LAUNCHER_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
