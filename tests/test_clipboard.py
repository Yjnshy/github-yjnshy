"""系统剪贴板读写与 /api/clipboard 接口（桌面窗口右键菜单的后端）。

真实 Win32 剪贴板只在显式设 ``DLY_TEST_REAL_CLIPBOARD=1`` 时才测：
它会短暂改动系统剪贴板，默认不该打扰正在用电脑的人。
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

from dly.core import clipboard
from dly.gui import server as srv


# ---- 跨平台分发与错误约定 ----

def test_non_windows_returns_unsupported(monkeypatch):
    monkeypatch.setattr(clipboard, "IS_WINDOWS", False)
    assert clipboard.read_text() is None
    assert clipboard.write_text("x") is False


def test_windows_dispatches_to_native(monkeypatch):
    monkeypatch.setattr(clipboard, "IS_WINDOWS", True)
    monkeypatch.setattr(clipboard, "_windows_read", lambda: "https://v.douyin.com/abc")
    monkeypatch.setattr(clipboard, "_windows_write", lambda t: t == "abc")
    assert clipboard.read_text() == "https://v.douyin.com/abc"
    assert clipboard.write_text("abc") is True


def test_empty_clipboard_is_not_the_same_as_failure(monkeypatch):
    """空剪贴板是 ''，失败才是 None —— 前端据此决定是否退回浏览器 API。"""
    monkeypatch.setattr(clipboard, "IS_WINDOWS", True)
    monkeypatch.setattr(clipboard, "_windows_read", lambda: "")
    assert clipboard.read_text() == ""


def test_native_errors_are_swallowed(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("剪贴板被占用")

    monkeypatch.setattr(clipboard, "IS_WINDOWS", True)
    monkeypatch.setattr(clipboard, "_windows_read", boom)
    monkeypatch.setattr(clipboard, "_windows_write", boom)
    assert clipboard.read_text() is None
    assert clipboard.write_text("x") is False


# ---- HTTP 接口 ----

@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(srv, "DEFAULT_PORT", 8911)   # 避开正式实例的 8765
    ctx = srv.create_app(desktop=True)
    ctx.start()
    try:
        yield ctx
    finally:
        ctx.shutdown()


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def _post(base: str, path: str, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def test_get_returns_clipboard_text(app, monkeypatch):
    monkeypatch.setattr(clipboard, "read_text", lambda: "整段分享文案 https://xxx")
    assert _get(app.url, "/api/clipboard") == (
        200, {"ok": True, "text": "整段分享文案 https://xxx"})


def test_get_marks_backend_unavailable(app, monkeypatch):
    monkeypatch.setattr(clipboard, "read_text", lambda: None)
    assert _get(app.url, "/api/clipboard")[1] == {"ok": False, "text": ""}


def test_post_writes_clipboard(app, monkeypatch):
    seen = {}

    def fake(text):
        seen["text"] = text
        return True

    monkeypatch.setattr(clipboard, "write_text", fake)
    assert _post(app.url, "/api/clipboard", {"text": "复制内容"})[1] == {"ok": True}
    assert seen["text"] == "复制内容"


def test_post_rejects_non_string_or_failed_write(app, monkeypatch):
    monkeypatch.setattr(clipboard, "write_text", lambda t: False)
    assert _post(app.url, "/api/clipboard", {"text": 123})[1] == {"ok": False}
    assert _post(app.url, "/api/clipboard", {"text": "x"})[1] == {"ok": False}


def test_desktop_flag_is_injected(app):
    with urllib.request.urlopen(app.url, timeout=5) as res:
        html = res.read().decode("utf-8")
    assert "window.DLY_DESKTOP=true;" in html
    assert "<!--DLY_DESKTOP_FLAG-->" not in html


def test_browser_mode_keeps_native_context_menu(monkeypatch):
    monkeypatch.setattr(srv, "DEFAULT_PORT", 8912)
    ctx = srv.create_app(desktop=False)
    ctx.start()
    try:
        with urllib.request.urlopen(ctx.url, timeout=5) as res:
            html = res.read().decode("utf-8")
        assert "window.DLY_DESKTOP=false;" in html
    finally:
        ctx.shutdown()


# ---- 真实 Win32 往返（默认跳过，需显式开启） ----

@pytest.mark.skipif(
    not clipboard.IS_WINDOWS or os.environ.get("DLY_TEST_REAL_CLIPBOARD") != "1",
    reason="会短暂改动系统剪贴板，需 DLY_TEST_REAL_CLIPBOARD=1",
)
def test_windows_roundtrip_and_restore():
    original = clipboard._windows_read()
    try:
        assert clipboard._windows_write("帅羊羊DLY 剪贴板测试 abc123") is True
        assert clipboard._windows_read() == "帅羊羊DLY 剪贴板测试 abc123"
    finally:
        clipboard._windows_write(original)
