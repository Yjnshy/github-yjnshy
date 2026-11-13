"""运行路径解析测试：源码运行 / 打包运行两种形态。

打包后最容易出的两类事故都在这里守住：

1. 找不到随包分发的 ffmpeg —— 用户机器上「录制一直失败」
2. 把日志/下载写进 Program Files —— 没权限，直接崩
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dly.core import ffmpeg as ffmpeg_mod
from dly.core import paths


def test_not_frozen_by_default():
    assert paths.is_frozen() is False


def test_resource_dirs_include_cwd():
    dirs = paths.resource_dirs()
    assert Path.cwd() in dirs
    # 去重且保持顺序
    assert len(dirs) == len(set(dirs))


def test_default_output_dir_under_cwd():
    assert paths.default_output_dir() == Path.cwd() / "downloads"


def test_resolve_path_keeps_absolute(tmp_path):
    target = tmp_path / "视频"
    assert paths.resolve_path(str(target)) == str(target)


def test_resolve_path_anchors_relative_to_app_dir():
    got = Path(paths.resolve_path("downloads"))
    assert got.is_absolute()
    assert got.name == "downloads"
    assert got.parent == paths.app_dir()


def test_ensure_dir_falls_back_when_unwritable(tmp_path):
    """目录建不出来时要退回临时目录，而不是抛异常让程序启动失败。

    用「把普通文件当目录用」制造真实的建目录失败，不依赖权限设置。
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")

    result = paths.ensure_dir(blocker / "logs")
    assert result.exists()
    assert result != blocker / "logs"


# ------------------------------------------------------------- ffmpeg 查找

def test_find_ffmpeg_prefers_bundled(tmp_path, monkeypatch):
    """随包分发的 ffmpeg 优先级高于 PATH（版本可控）。"""
    bundled = tmp_path / "bin"
    bundled.mkdir()
    fake = bundled / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    fake.write_bytes(b"x")

    monkeypatch.setattr(paths, "resource_dirs", lambda: [tmp_path])
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda _n: r"C:\path\ffmpeg.exe")

    assert ffmpeg_mod.find_ffmpeg("") == str(fake)


def test_find_ffmpeg_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "resource_dirs", lambda: [tmp_path])
    monkeypatch.setattr(ffmpeg_mod.shutil, "which",
                        lambda name: rf"C:\somewhere\{name}.exe")
    assert ffmpeg_mod.find_ffmpeg("").endswith("ffmpeg.exe")


def test_configured_ffmpeg_wins(tmp_path, monkeypatch):
    custom = tmp_path / "my-ffmpeg.exe"
    custom.write_bytes(b"x")
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda _n: None)
    assert ffmpeg_mod.find_ffmpeg(str(custom)) == str(custom)


def test_find_ffprobe_next_to_ffmpeg(tmp_path):
    ff = tmp_path / "ffmpeg.exe"
    ff.write_bytes(b"x")
    probe = tmp_path / "ffprobe.exe"
    probe.write_bytes(b"x")
    assert ffmpeg_mod.find_ffprobe(str(ff)) == str(probe)


@pytest.mark.parametrize("missing", ["", None])
def test_find_ffmpeg_returns_none_when_absent(missing, monkeypatch):
    monkeypatch.setattr(paths, "resource_dirs", lambda: [])
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda _n: None)
    monkeypatch.setattr(ffmpeg_mod.Path, "is_file",
                        lambda self: False, raising=False)
    assert ffmpeg_mod.find_ffmpeg(missing) is None
