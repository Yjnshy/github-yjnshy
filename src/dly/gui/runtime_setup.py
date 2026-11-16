"""检测并自动安装桌面窗口依赖的 Windows 运行时。

桌面窗口（pywebview + pythonnet）依赖两样系统组件：

- **Microsoft Edge WebView2 运行时**：渲染界面用；Win11 / 新版 Win10 自带
- **Microsoft .NET Framework 4.8**：pythonnet 加载 CLR 用；Win10 1903+ 一般自带

老系统、LTSC、精简版系统常常缺其中一样，表现为启动报
``Failed to resolve Python.Runtime.Loader.Initialize`` 或提示装 WebView2。

这里的职责：查注册表判断缺哪个 → 下载微软官方安装包 → 以管理员身份静默安装。
安装包必须提权，UAC 那一次「是」绕不过去；装完由调用方重启程序（pythonnet
初始化失败的状态会留在当前进程里，必须换新进程才干净）。
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import List, Optional

try:                       # 非 Windows 环境导入本模块只为跑测试，不应炸
    import winreg
except ImportError:        # pragma: no cover - 项目只面向 Windows
    winreg = None  # type: ignore[assignment]

logger = logging.getLogger("dly")

# 微软官方永久链接（fwlink 会跳转到当期版本，链接本身长期有效）。
# 用体积小的联网安装包（1~2MB），大安装包不再随 exe / zip 分发。
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
DOTNET48_URL = "https://go.microsoft.com/fwlink/?LinkId=2085155"

# 让用户自己装时打开的官方下载页面
DOWNLOAD_PAGES = {
    "dotnet48": "https://dotnet.microsoft.com/download/dotnet-framework/net48",
    "webview2": "https://developer.microsoft.com/microsoft-edge/webview2/",
}

WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
DOTNET48_MIN_RELEASE = 528040        # .NET 4.8 = 528040；4.8.1 = 533320

LABELS = {
    "dotnet48": "Microsoft .NET Framework 4.8",
    "webview2": "Microsoft Edge WebView2 运行时",
}

# 组件 → (下载地址, 临时文件名, 静默安装参数)
INSTALLERS = {
    "dotnet48": (DOTNET48_URL, "ndp48-x86-x64-allos-enu.exe", "/q /norestart"),
    "webview2": (WEBVIEW2_URL, "MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
                 "/silent /install"),
}

# 安装器的"已成功但有话说"退出码：3010 = 需要重启，1641 = 已安排重启
_OK_CODES = (0, 3010, 1641)
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_WM_CLOSE = 0x0010
_IDYES = 6
_IDNO = 7


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIcon", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


class _Note:
    """非阻塞提示框：后台线程弹 MessageBox，close() 时按标题把它关掉。

    下载 / 静默安装期间不能让界面看起来「卡死」，又不想为进度条引入 GUI
    依赖，所以用系统消息框当"临时状态栏"。
    """

    def __init__(self, text: str, title: str = "帅羊羊DLY") -> None:
        self.title = title
        self._thread = threading.Thread(target=self._show, args=(text,),
                                        daemon=True, name="setup-note")
        self._thread.start()
        time.sleep(0.35)          # 给窗口一点时间真正弹出来

    def _show(self, text: str) -> None:
        try:
            ctypes.windll.user32.MessageBoxW(None, text, self.title, 0x40)
        except Exception:         # noqa: BLE001 弹不出来不影响主流程
            logger.debug("提示框创建失败", exc_info=True)

    def close(self) -> None:
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, self.title)
            if hwnd:
                ctypes.windll.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        except Exception:         # noqa: BLE001 关不掉就留着，用户自己点确定
            logger.debug("提示框关闭失败", exc_info=True)


def _reg_value(root: int, path: str, name: str):
    """读注册表值；不存在 / 无权限一律返回 None。"""
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(root, path, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except OSError:
        return None


def has_dotnet48() -> bool:
    """注册表 Release ≥ 528040 表示 .NET Framework 4.8 已安装。"""
    value = _reg_value(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full",
        "Release") if winreg else None
    try:
        return int(value) >= DOTNET48_MIN_RELEASE
    except (TypeError, ValueError):
        return False


def has_webview2() -> bool:
    """WebView2 运行时按机器装（HKLM）或按用户装（HKCU），两处都查。"""
    if winreg is None:
        return False
    paths = [
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
        (winreg.HKEY_CURRENT_USER,
         rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
    ]
    for root, path in paths:
        pv = _reg_value(root, path, "pv")
        if isinstance(pv, str) and pv.strip() and pv.strip() != "0.0.0.0":
            return True
    return False


def missing_components() -> List[str]:
    """返回缺失组件名列表（空列表 = 两样都在，不用装）。"""
    missing = []
    if not has_dotnet48():
        missing.append("dotnet48")
    if not has_webview2():
        missing.append("webview2")
    return missing


def looks_like_runtime_failure(exc_text: str) -> bool:
    """桌面报错是不是 .NET / WebView2 这类环境问题。

    朋友那台机器注册表显示 .NET 4.8 已装，但桌面仍报
    ``Failed to resolve Python.Runtime.Loader.Initialize`` —— 说明"齐全"
    不代表可用，这种错误必须让用户有机会强制修复安装一次。
    """
    text = (exc_text or "").lower()
    return any(token in text for token in (
        "python.runtime", "pythonnet", "webview2", "clr", ".net",
        "desktop", "failed to resolve",
    ))


def repair_targets() -> List[str]:
    """强制修复时装哪几样：两样都装，避免反复试错。"""
    return ["dotnet48", "webview2"]


def prompt_runtime_action(missing: List[str],
                          repair: bool = False) -> str:
    """三选一：'install' 自动装 / 'pages' 打开官方页面 / 'skip' 跳过。"""
    if not missing:
        return "skip"
    names = "、".join(LABELS.get(m, m) for m in missing)
    if repair:
        text = (
            "桌面窗口启动失败：系统里虽然检测到了运行环境，但组件可能已损坏。\n"
            f"建议重新安装：{names}。\n\n"
            "【是】自动联网下载安装包并静默安装（1~2MB 引导包，装完自动重开）\n"
            "【否】打开微软官方下载页面，自己手动安装\n"
            "【取消】不装了，改用浏览器模式继续运行\n\n"
            "无论哪种安装方式，Windows 都会要求确认一次「用户账户控制」。"
        )
    else:
        text = (
            f"这台电脑缺少桌面窗口所需的系统组件：{names}\n\n"
            "【是】自动联网下载安装包并静默安装（1~2MB 引导包，装完自动重开）\n"
            "【否】打开微软官方下载页面，自己手动安装\n"
            "【取消】不装了，改用浏览器模式继续运行（功能相同，界面在浏览器里）\n\n"
            "安装包不会随程序附带，以保持压缩包体积小巧。"
        )
    try:
        res = ctypes.windll.user32.MessageBoxW(
            None, text, "帅羊羊DLY - 运行环境提示", 0x03 | 0x30)  # 是/否/取消
    except Exception:         # noqa: BLE001 弹不出窗就按「跳过」处理
        logger.warning("询问安装弹窗失败", exc_info=True)
        return "skip"
    return {_IDYES: "install", _IDNO: "pages"}.get(res, "skip")


def open_download_pages(missing: List[str]) -> None:
    """在系统默认浏览器里打开缺失组件的微软官方下载页面。"""
    import webbrowser

    for name in missing:
        url = DOWNLOAD_PAGES.get(name)
        if not url:
            continue
        logger.info("打开官方下载页面：%s", url)
        try:
            webbrowser.open(url)
        except Exception:     # noqa: BLE001 打不开也不能影响浏览器模式回退
            logger.warning("打开下载页面失败：%s", url, exc_info=True)


def _download(url: str, dest: Path, timeout: float = 60.0) -> bool:
    """流式下载到 dest；文件太小（多半是错误页）视为失败。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DLY/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, \
                open(dest, "wb") as fh:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
    except Exception:         # noqa: BLE001 网络问题交给调用方兜底
        logger.warning("下载失败：%s", url, exc_info=True)
        return False
    size = dest.stat().st_size if dest.exists() else 0
    if size < 1024 * 1024:    # 安装包至少几十 MB，1MB 以下必然是错误页
        logger.warning("下载内容过小（%s 字节），判定失败", size)
        return False
    return True


def _run_elevated(exe: Path, params: str,
                  timeout: float = 1800.0) -> Optional[int]:
    """以管理员身份运行并等待结束；用户点了 UAC「否」返回 None。"""
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(exe)
    info.lpParameters = params
    info.nShow = 1
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        logger.warning("提权启动失败（用户拒绝 UAC 或系统限制）")
        return None
    handle = info.hProcess
    try:
        ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout * 1000))
        code = ctypes.c_ulong(0)
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return int(code.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def install(missing: List[str], temp_dir: Optional[Path] = None) -> bool:
    """依次下载并静默安装缺失组件；返回安装后是否已全部就位。"""
    if not missing:
        return True
    work = Path(temp_dir or tempfile.gettempdir()) / "dly_setup"
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("临时目录不可用：%s", work, exc_info=True)
        return False

    for name in missing:
        url, filename, params = INSTALLERS[name]
        label = LABELS.get(name, name)
        dest = work / filename

        note = _Note(f"正在下载 {label} 安装包…\n\n约 1~2MB，几秒到几十秒，请勿关闭本程序。")
        got = _download(url, dest) or _download(url, dest)   # 失败重试一次
        note.close()
        if not got:
            logger.warning("%s 下载失败", label)
            continue

        note = _Note(
            f"正在安装 {label}…\n\n"
            "屏幕上可能暂时没有变化，这是静默安装的正常表现，通常 1~3 分钟。\n"
            "若弹出「用户账户控制」，请点「是」。")
        code = _run_elevated(dest, params)
        note.close()
        if code is None:
            logger.warning("%s 安装被取消（UAC 未确认）", label)
            break
        if code not in _OK_CODES:
            logger.warning("%s 安装器退出码 %s", label, code)
            continue
        logger.info("%s 安装完成，退出码 %s", label, code)

    try:                      # 安装包不再需要，别在用户临时目录里留 100MB+
        for name in missing:
            leftover = work / INSTALLERS[name][1]
            if leftover.exists():
                leftover.unlink()
    except OSError:
        logger.debug("清理安装包失败", exc_info=True)

    # 以注册表为准复核：安装器报成功但没落盘的情况确实存在
    still = [c for c in missing if c in missing_components()]
    if still:
        logger.warning("安装后仍缺失：%s", still)
        return False
    return True


def relaunch() -> None:
    """用新进程重新启动自己（当前进程里 pythonnet 的失败状态不会被重置）。"""
    exe = sys.executable
    args = [a for a in sys.argv[1:] if a != "--web"]
    logger.info("安装完成后重新启动：%s", exe)
    try:
        subprocess.Popen([exe, *args], close_fds=True)
    except OSError:
        logger.warning("重新启动失败，请手动打开 DLY.exe", exc_info=True)
        raise SystemExit(1)
