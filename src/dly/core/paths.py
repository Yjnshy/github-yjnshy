"""运行路径解析：区分「源码运行」与「PyInstaller 打包后运行」。

打包后有两类目录，混用会导致「本机能用、发给别人就打不开」：

- **资源目录**：ffmpeg、内置页面等随程序分发的文件。PyInstaller 会把它们
  解到 ``sys._MEIPASS``（单文件模式）或 exe 同级目录（文件夹模式）
- **数据目录**：日志、下载产物等运行期可写的文件。绝不能写进 exe 所在目录，
  因为程序可能被装在 ``C:\\Program Files`` 这种无写权限的位置

本模块只做路径推导，不创建目录（除显式调用 :func:`ensure_dir`）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

APP_NAME = "DLY"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打好的包里。"""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """只读资源根目录。

    - 单文件 exe：PyInstaller 解压出来的临时目录 ``sys._MEIPASS``
    - 文件夹模式 exe：exe 所在目录（数据被打散放在同级）
    - 源码运行：项目根目录
    """
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        return Path(meipass)
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def app_dir() -> Path:
    """程序所在目录：打包后是 exe 目录，源码运行时是当前工作目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def resource_dirs() -> List[Path]:
    """可能存放随处分发文件（ffmpeg 等）的目录，按优先级排列。"""
    dirs: List[Path] = [bundle_dir()]
    if is_frozen():
        exe_dir = app_dir()
        dirs += [exe_dir, exe_dir / "bin", exe_dir / "runtime"]
    else:
        dirs += [app_dir(), app_dir() / "bin", app_dir() / "runtime"]
    seen: List[Path] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return seen


def _writable(path: Path) -> bool:
    """真实写一次才算可写：能建目录不代表能建文件。"""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        probe = Path(path) / ".dly_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


_DATA_DIR_CACHE: Optional[Path] = None


def data_dir() -> Path:
    """可写的用户数据目录（日志、默认下载位置）。

    打包后优先 ``%LOCALAPPDATA%\\DLY``；但实测某些机器上冻结进程写这里会被
    系统拒绝（PermissionError 13，同一个目录普通 python 进程却正常），而
    程序目录一直可写——所以这里实际探一次，不行就退到 exe 同级目录，
    再不行退临时目录。源码运行仍用当前目录，保持开发时行为直观。
    """
    global _DATA_DIR_CACHE
    if _DATA_DIR_CACHE is not None:
        return _DATA_DIR_CACHE

    if not is_frozen():
        _DATA_DIR_CACHE = Path.cwd()
        return _DATA_DIR_CACHE

    candidates = []
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    candidates.append(Path(base) / APP_NAME)
    try:
        candidates.append(Path(sys.executable).resolve().parent)
    except OSError:
        pass

    for candidate in candidates:
        if _writable(candidate):
            _DATA_DIR_CACHE = candidate
            return candidate

    _DATA_DIR_CACHE = ensure_dir(Path(tempfile.gettempdir()) / APP_NAME)
    return _DATA_DIR_CACHE


def ensure_dir(path: Path) -> Path:
    """尽力创建目录；失败则退回临时目录，保证程序还能跑。"""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return Path(path)
    except OSError:
        fallback = Path(tempfile.gettempdir()) / APP_NAME
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def log_dir() -> Path:
    """日志目录：数据目录优先，不可写时用临时目录。"""
    return ensure_dir(data_dir() / "logs")


def default_output_dir() -> Path:
    """默认下载目录：打包后落在数据目录，源码运行落在 ./downloads。"""
    if not is_frozen():
        return Path.cwd() / "downloads"
    return ensure_dir(data_dir() / "downloads")


def resolve_path(value: str) -> str:
    """把配置里的相对路径按「程序目录」解析，避免受启动方式影响。"""
    if not value:
        return value
    p = Path(value)
    if p.is_absolute():
        return str(p)
    return str((app_dir() / p).resolve())
