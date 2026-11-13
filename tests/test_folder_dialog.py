"""选择文件夹对话框：只测跨平台分发与参数整理，不真的弹窗。"""

from __future__ import annotations

import os

from dly.core import folder_dialog as fd


def test_windows_path_is_used_and_arguments_trimmed(monkeypatch):
    seen = {}

    def fake(initial: str, title: str) -> str:
        seen["args"] = (initial, title)
        return r"D:\影片"

    monkeypatch.setattr(fd, "IS_WINDOWS", True)
    monkeypatch.setattr(fd, "_windows_pick_folder", fake)
    assert fd.pick_folder("  D:\\影片  ", "选择保存目录") == r"D:\影片"
    assert seen["args"] == (r"D:\影片", "选择保存目录")


def test_cancel_returns_empty_string(monkeypatch):
    monkeypatch.setattr(fd, "IS_WINDOWS", True)
    monkeypatch.setattr(fd, "_windows_pick_folder", lambda initial, title: "")
    assert fd.pick_folder("", "选择保存目录") == ""


def test_initial_empty_means_no_hint(monkeypatch):
    seen = {}

    def fake(initial: str, title: str) -> str:
        seen["initial"] = initial
        return ""

    monkeypatch.setattr(fd, "IS_WINDOWS", True)
    monkeypatch.setattr(fd, "_windows_pick_folder", fake)
    fd.pick_folder("   ", "选择保存目录")
    assert seen["initial"] == ""


def test_home_shorthand_is_expanded(monkeypatch):
    seen = {}

    def fake(initial: str, title: str) -> str:
        seen["initial"] = initial
        return ""

    monkeypatch.setattr(fd, "IS_WINDOWS", True)
    monkeypatch.setattr(fd, "_windows_pick_folder", fake)
    fd.pick_folder("~/videos", "选择保存目录")
    assert seen["initial"] == os.path.expanduser("~/videos")
    assert "~" not in seen["initial"]
