"""统一日志：控制台 rich 输出 + 文件轮转。

打包成窗口程序后没有控制台（``sys.stdout`` 为 ``None``），此时**只能**写文件，
否则第一条日志就会抛 ``AttributeError: 'NoneType' object has no attribute
'write'``——这类异常还会因为无处可打而彻底静默，非常难查。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from rich.logging import RichHandler

from dly.core import paths

_INITIALIZED = False


def _console_available() -> bool:
    """窗口模式（pyinstaller --noconsole）下没有标准输出。"""
    return sys.stdout is not None and sys.stderr is not None


def setup_logging(level: int = logging.INFO,
                  log_dir: Optional[str] = None,
                  console: Optional[bool] = None) -> Path:
    """初始化日志，返回实际使用的日志目录。

    - ``log_dir`` 省略时按运行方式推导（源码为 ./logs，打包后为数据目录）
    - ``console`` 省略时自动判断：没有标准输出就只写文件
    """
    global _INITIALIZED
    root = logging.getLogger("dly")
    root.setLevel(level)

    want_console = _console_available() if console is None else console
    if not _INITIALIZED:
        _INITIALIZED = True
        if want_console and not any(isinstance(h, RichHandler)
                                    for h in root.handlers):
            handler = RichHandler(rich_tracebacks=True, show_path=False)
            handler.setFormatter(logging.Formatter("%(message)s"))
            root.addHandler(handler)

    target = Path(log_dir) if log_dir else paths.log_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        target = paths.ensure_dir(paths.data_dir() / "logs")

    # 文件日志只加一次，重复调用（例如界面里多次 create_app）不会重复写
    logfile = (target / "dly.log").resolve()
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler) and \
                Path(h.baseFilename).resolve() == logfile:
            return target
    try:
        file_handler = RotatingFileHandler(
            logfile, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(file_handler)
    except OSError:
        pass   # 日志目录不可写时不影响主流程
    return target
