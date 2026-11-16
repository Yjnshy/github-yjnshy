"""让 pywebview 的窗口在打包版里也能起来（Windows / .NET Core 路线）。

背景：pywebview 在 Windows 上依赖 pythonnet → .NET。打包成 exe 后，
.NET Framework（netfx）那条路的程序集加载会失败（``Failed to resolve
Python.Runtime.Loader.Initialize``），而 .NET Core（coreclr）这条路能正常
加载——但 pywebview 6.x 的 winforms 后端有两处只兼容 .NET Framework 的写法，
需要一份打过补丁的副本（``packaging/patch_pywebview.py`` 生成，随包分发）。

这里做三件事，必须在 ``webview.start()`` 之前调用：

1. 指定用 coreclr，并给一份带 ``Microsoft.WindowsDesktop.App`` 的
   runtimeconfig（不然 WinForms 程序集根本不在探测范围内）
2. 先 ``pythonnet.load("coreclr")``，失败就干净地退回默认运行时
3. 把补丁副本注册成 ``webview.platforms.winforms``

coreclr 不可用时（机器上只有 .NET Framework）不抛异常，交给 pywebview
自己按默认运行时尝试；真起不来由 ``desktop.launch`` 转成
``DesktopUnavailable``，上层再退回浏览器界面。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

BACKEND_NAME = "webview.platforms.winforms"
RUNTIME_CONFIG_NAME = "dotnet_desktop.runtimeconfig.json"
BACKEND_PATCH_NAME = "pywebview_winforms_coreclr.py"

_prepared = False


def _patch_dir() -> Path:
    """补丁文件所在目录：打包版在 _internal/dly_patch，源码版在 packaging/。"""
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        return Path(meipass) / "dly_patch"
    return Path(__file__).resolve().parents[3] / "packaging"


def _install_patched_backend() -> bool:
    """注册补丁版 winforms 后端；没有补丁文件就保持原样。"""
    patch_file = _patch_dir() / BACKEND_PATCH_NAME
    if not patch_file.is_file():
        logger.debug("未找到 winforms 补丁副本：%s", patch_file)
        return False

    import webview
    import webview.platforms  # noqa: F401  确保父包已导入

    spec = importlib.util.spec_from_file_location(BACKEND_NAME, patch_file)
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    # 以 webview.platforms.winforms 的身份执行，模块里的相对导入才成立
    module.__package__ = "webview.platforms"
    sys.modules[BACKEND_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # 补丁执行失败要撤干净，否则 pywebview 会拿到一个残缺模块
        sys.modules.pop(BACKEND_NAME, None)
        raise
    webview.platforms.winforms = module
    logger.debug("已启用 winforms 后端补丁：%s", patch_file)
    return True


def prepare() -> None:
    """按需初始化 .NET 运行时（幂等，非 Windows 直接返回）。"""
    global _prepared
    if _prepared or sys.platform != "win32":
        return
    _prepared = True

    runtime_config = _patch_dir() / RUNTIME_CONFIG_NAME
    os.environ["PYTHONNET_RUNTIME"] = "coreclr"
    if runtime_config.is_file():
        os.environ["PYTHONNET_CORECLR_RUNTIME_CONFIG"] = str(runtime_config)

    try:
        import pythonnet

        pythonnet.load("coreclr")
    except Exception as exc:  # noqa: BLE001 机器上没有 .NET Core/5+ 桌面运行时
        logger.warning("coreclr 不可用（%s），退回默认 .NET 运行时", exc)
        os.environ.pop("PYTHONNET_RUNTIME", None)
        os.environ.pop("PYTHONNET_CORECLR_RUNTIME_CONFIG", None)
        try:
            import pythonnet as _pn

            _pn._RUNTIME = None     # 让 pywebview 能重新选择运行时
            _pn._LOADED = False
        except Exception:           # noqa: BLE001 私有属性变了也不影响后续尝试
            pass
        return

    try:
        _install_patched_backend()
    except Exception:  # noqa: BLE001 补丁没装上也还能试原版
        logger.warning("winforms 后端补丁未生效", exc_info=True)
