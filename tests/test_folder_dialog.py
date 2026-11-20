"""选择文件夹对话框：只测跨平台分发与参数整理，不真的弹窗。"""

from __future__ import annotations

import ctypes
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


def test_pick_media_files_dispatches_and_trims(monkeypatch):
    seen = {}

    def fake(initial: str, title: str):
        seen["args"] = (initial, title)
        return ["a.mp4", "b.mp4"]

    monkeypatch.setattr(fd, "IS_WINDOWS", True)
    monkeypatch.setattr(fd, "_windows_pick_media_files", fake)
    assert fd.pick_media_files("  D:/videos  ", "选视频") == ["a.mp4", "b.mp4"]
    assert seen["args"] == ("D:/videos", "选视频")


def test_windows_pick_media_files_com_vtable(monkeypatch):
    """守住 COM 虚表方法序号：SetFileTypes=4、SetFileTypeIndex=5。

    曾经把 SetFileTypeIndex 写成 6（实为 GetFileTypeIndex，参数是指针），
    传整数会触发访问违规被 except 吞掉 → 对话框静默失败、按钮点了没反应。
    """
    calls = []

    class FakeOle:
        def CoInitializeEx(self, *a):
            return 0

        def CoCreateInstance(self, *a):
            return 0

        def CoTaskMemFree(self, buf):
            pass

        def CoUninitialize(self):
            pass

    class FakeUser32:
        def GetForegroundWindow(self):
            return 1

    def fake_guid(text):
        return ctypes.c_uint32(1)

    def fake_method(ptr, index, *argtypes):
        def call(this, *args):
            calls.append(index)
            if index == 10:            # GetOptions → S_OK，回填 options
                if args and hasattr(args[0], "value"):
                    args[0].value = 0
                return 0
            if index == 3:             # Show → 非 S_OK，函数提前返回 []
                return -2147221164
            return 0
        return call

    class FakeWindll:
        ole32 = FakeOle()
        user32 = FakeUser32()
        shell32 = fd.ctypes.windll.shell32

    monkeypatch.setattr(fd.ctypes, "windll", FakeWindll())
    monkeypatch.setattr(fd, "_guid", fake_guid)
    monkeypatch.setattr(fd, "_method", fake_method)
    monkeypatch.setattr(fd, "_release", lambda p: None)

    result = fd._windows_pick_media_files("", "选视频")
    assert result == []

    # 关键：设文件类型用 4/5，绝不能出现会崩的 6（GetFileTypeIndex）
    assert 4 in calls, "SetFileTypes(4) 必须被调用"
    assert 5 in calls, "SetFileTypeIndex(5) 必须被调用"
    assert 6 not in calls, "不能调用 6=GetFileTypeIndex（传整数会访问违规）"
