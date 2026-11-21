# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：产出 dist/DLY/DLY.exe。

用法（在项目根目录）：
    pyinstaller packaging/dly.spec --noconfirm

或直接双击 / 运行 ``scripts/打包exe.bat``。

为什么用「文件夹模式」（onedir）而不是单文件（onefile）：

* 单文件 exe 每次启动都要把上百 MB 内容解压到临时目录，冷启动要好几秒，
  而且杀毒软件对这种「自解压」行为特别敏感，容易被误报
* 文件夹模式启动快、体积也小一点，用户拿到的是一个文件夹，双击里面的
  exe 即可，不影响「免安装」的体验

ffmpeg 默认随包分发（约 190MB），不想带就设 ``DLY_BUNDLE_FFMPEG=0``，
此时程序会退回到用系统 PATH 里的 ffmpeg。
"""

import os
import re
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).resolve().parent          # noqa: F821 spec 注入变量
SRC = ROOT / "src"
PKG = SRC / "dly"
APP_NAME = "DLY"

sys.path.insert(0, str(SRC))


def package_version() -> str:
    text = (PKG / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def find_tool(name: str) -> Path | None:
    """定位 ffmpeg / ffprobe：环境变量 > PATH > 已知本地位置。"""
    override = os.environ.get(f"DLY_{name.upper()}", "").strip()
    if override and Path(override).is_file():
        return Path(override)
    found = shutil.which(name)
    if found:
        return Path(found)
    for candidate in (rf"C:\ffmpeg\bin\{name}.exe",
                      rf"E:\Program Files (x86)\ytdlp\bin\{name}.exe"):
        if Path(candidate).is_file():
            return Path(candidate)
    return None


def write_version_file() -> str:
    """生成 exe 文件属性里的版本信息（右键属性能看到，避免显示 0.0.0.0）。"""
    version = package_version()
    parts = [int(x) for x in re.findall(r"\d+", version)][:4]
    parts += [0] * (4 - len(parts))
    quad = tuple(parts)
    content = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={quad}, prodvers={quad},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'Yjnshy'),
        StringStruct('FileDescription', '帅羊羊DLY · 视频下载与直播录制'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('LegalCopyright', 'MIT License'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '帅羊羊DLY'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    target = ROOT / "build" / "version_info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return str(target)


# ---- 随包资源 ----

datas = [
    # 界面单页（server.py 通过 importlib.resources 读取）
    (str(PKG / "gui" / "assets"), "dly/gui/assets"),
    # 桌面窗口：.NET Core 运行配置 + pywebview winforms 后端补丁副本
    (str(ROOT / "packaging" / "dotnet_desktop.runtimeconfig.json"), "dly_patch"),
    (str(ROOT / "packaging" / "pywebview_winforms_coreclr.py"), "dly_patch"),
]
# pywebview 注入脚本；内置钩子版本较旧时可能漏掉 js/ 目录
datas += collect_data_files("webview", subdir="js")

binaries = []
if os.environ.get("DLY_BUNDLE_FFMPEG", "1") != "0":
    for tool in ("ffmpeg", "ffprobe"):
        path = find_tool(tool)
        if path:
            binaries.append((str(path), "bin"))
            print(f"[打包] 随附 {tool}: {path}")
        else:
            print(f"[打包] 未找到 {tool}，打包后需用户自行安装")

hiddenimports = [
    # 延迟导入的模块不会被静态分析发现
    "dly.gui.desktop",
    "dly.gui.login_window",
    "dly.gui.clr_boot",
    "dly.gui.server",
    "dly.downloader.engine",
    "dly.downloader.images",
    "dly.core.douyin_import",
    "dly.core.qishui_signer",   # 完整版签名桥接（函数内延迟导入）
    "dly.gui.dyimport_state",
    "websocket",
    "dly.recorder.recorder",
    "dly.recorder.monitor",
    "webview.platforms.winforms",
    "clr",
    # 图文作品里的 webp 转 jpg（可选依赖，装了才会打包进来）
    "PIL.Image",
]

excludes = [
    "tkinter", "matplotlib", "numpy", "pandas", "scipy",
    "pytest", "IPython", "notebook", "PyQt5", "PyQt6",
    "PySide2", "PySide6", "PyGObject", "gi",
]

a = Analysis(
    [str(ROOT / "packaging" / "app_main.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX 压缩极易被 360/Defender 误判，宁可不压
    console=False,             # 窗口程序，双击不弹黑框
    disable_windowed_traceback=False,
    icon=str(ROOT / "packaging" / "app.ico"),
    version=write_version_file(),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

# PyInstaller 会把收集到的二进制重新打包改写；在这台机器上 ffprobe.exe 被改写后
# 运行直接 0xC0000005（ffmpeg 正常）。构建完用原始可执行文件覆盖，
# 保证 ffprobe 能正常读时长/编码（下载和录制都依赖它）。
import shutil as _shutil

_out_bin = Path(DISTPATH) / APP_NAME / "_internal" / "bin"
if _out_bin.is_dir():
    for _tool in ("ffmpeg", "ffprobe"):
        _source_tool = find_tool(_tool)
        if _source_tool:
            _shutil.copy2(_source_tool, _out_bin / f"{_tool}.exe")
            print(f"[打包修复] 覆盖 {_tool}: {_source_tool}")
