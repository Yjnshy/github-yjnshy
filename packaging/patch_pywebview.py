"""从已安装的 pywebview 生成「.NET Core 可用」的 winforms 后端副本。

为什么需要这个补丁：

1. ``pywebview/platforms/winforms.py`` 里 ``from Microsoft.Win32 import SystemEvents``
   在 .NET Framework 下直接可用；换到 .NET Core（coreclr）后 SystemEvents 搬到了
   独立程序集 ``Microsoft.Win32.SystemEvents``，必须显式 ``clr.AddReference``。
2. 同一个文件里的 ``OpenFolderDialog`` 类在类体执行时就用
   ``System.Windows.Forms.FileDialogNative`` 这个类型，.NET Core 上取不到 → 直接
   AttributeError，整个模块 import 失败。本程序的文件夹选择走自己实现的
   ``dly.core.folder_dialog``，不需要它。

生成物 ``packaging/pywebview_winforms_coreclr.py`` 随包分发，运行时以
``webview.platforms.winforms`` 的名字注册（见 ``dly.gui.clr_boot``）。
pywebview 升级后重跑本脚本即可重新生成。

用法::

    python packaging/patch_pywebview.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "pywebview_winforms_coreclr.py"

SYSTEM_EVENTS_OLD = (
    "import System.Windows.Forms as WinForms  # noqa: E402\n"
    "from Microsoft.Win32 import SystemEvents  # noqa: E402"
)

SYSTEM_EVENTS_NEW = '''import System.Windows.Forms as WinForms  # noqa: E402

# .NET Core 下 SystemEvents 在独立程序集 Microsoft.Win32.SystemEvents 里，
# 原版 pywebview 没有加载（.NET Framework 时代它在 System.dll 中）。
try:
    clr.AddReference("Microsoft.Win32.SystemEvents")
except Exception:  # noqa: BLE001 退回按路径找 WindowsDesktop 运行时里的副本
    import glob as _glob
    import os as _os

    import string as _string
    _roots = []
    _env_root = _os.environ.get("DOTNET_ROOT", "").strip()
    if _env_root:
        _roots.append(_env_root)
    for _env in ("ProgramFiles", "ProgramFiles(x86)"):
        _pf = _os.environ.get(_env, "").strip()
        if _pf:
            _roots.append(_os.path.join(_pf, "dotnet"))
    for _letter in _string.ascii_uppercase:
        _roots.append(_os.path.join(_letter + ":", _os.sep, "Program Files", "dotnet"))
        _roots.append(_os.path.join(_letter + ":", _os.sep, "Program Files (x86)", "dotnet"))
    _hits = []
    for _root in _roots:
        _hits += _glob.glob(_os.path.join(
            _root, "shared", "Microsoft.WindowsDesktop.App", "*",
            "Microsoft.Win32.SystemEvents.dll"))
    if not _hits:
        raise
    clr.AddReference(sorted(_hits)[-1])
from Microsoft.Win32 import SystemEvents  # noqa: E402'''

STUB = '''class OpenFolderDialog:  # patched: 原实现依赖 .NET Framework 的 FileDialogNative
    @classmethod
    def show(cls, *args, **kwargs):
        raise RuntimeError("请使用 dly.core.folder_dialog 选择文件夹")
'''


def locate_source() -> Path:
    import webview

    return Path(webview.__file__).parent / "platforms" / "winforms.py"


def patch_source(src: str) -> tuple[str, int]:
    if SYSTEM_EVENTS_OLD not in src:
        raise SystemExit("✗ 没找到 SystemEvents 导入段落，pywebview 版本可能变了")
    src = src.replace(SYSTEM_EVENTS_OLD, SYSTEM_EVENTS_NEW)

    lines = src.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    removed = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("class OpenFolderDialog"):
            removed += 1
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() == "" or nxt[:1] in (" ", "\t"):
                    i += 1
                    continue
                break
            out.append(STUB)
            out.append("\n\n")
            continue
        out.append(line)
        i += 1
    if removed != 1:
        raise SystemExit(f"✗ OpenFolderDialog 命中 {removed} 次（应为 1）")
    return "".join(out), removed


def main() -> int:
    src_path = locate_source()
    text, removed = patch_source(src_path.read_text(encoding="utf-8"))
    header = (
        'r"""pywebview winforms 后端的 .NET Core 补丁副本 —— 自动生成，勿手改。\n\n'
        f"来源：{src_path.as_posix()}\n"
        "生成：python packaging/patch_pywebview.py\n"
        '"""\n'
    )
    OUT.write_text(header + text, encoding="utf-8")
    print(f"✓ 生成 {OUT}")
    print(f"  来源 {src_path}")
    print(f"  替换 SystemEvents 导入 1 处，移除 OpenFolderDialog {removed} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
