"""站点登录会话：存储、优先级、登录窗判定逻辑。"""

from __future__ import annotations

import pytest

from dly.cookies import manager as manager_mod
from dly.cookies import sessions
from dly.core.config import CookieConfig
from dly.gui import login_window


@pytest.fixture()
def store(tmp_path, monkeypatch):
    path = tmp_path / "login_sessions.json"
    monkeypatch.setattr(sessions, "STORE_PATH", path)
    return path


def test_login_session_roundtrip(store):
    sessions.save("bilibili", "SESSDATA=abc; bili_jct=1")
    assert sessions.get_header("bilibili") == "SESSDATA=abc; bili_jct=1"
    assert store.is_file()


def test_status_never_leaks_cookie_values(store):
    sessions.save("douyin", "sessionid=secret-value")
    status = {s["platform"]: s for s in sessions.status()}
    assert status["douyin"]["logged_in"] is True
    assert "secret-value" not in str(status)
    assert status["bilibili"]["logged_in"] is False


def test_clear_removes_session(store):
    sessions.save("bilibili", "SESSDATA=abc")
    assert sessions.clear("bilibili") is True
    assert sessions.get_header("bilibili") == ""
    assert sessions.clear("bilibili") is False


def test_empty_header_is_ignored(store):
    sessions.save("bilibili", "   ")
    assert sessions.status()[0]["logged_in"] is False


def test_login_session_beats_manual_and_file(store, tmp_path):
    sessions.save("bilibili", "SESSDATA=from-login")
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tfrom-file\n",
        encoding="utf-8")

    config = CookieConfig(cookies_file=str(cookie_file),
                          manual_cookie="SESSDATA=from-manual")
    mgr = manager_mod.CookieManager(config, use_cache=False)
    assert "from-login" in mgr.get_header("bilibili")


def test_reload_clears_cached_state(store):
    mgr = manager_mod.CookieManager(CookieConfig(), use_cache=False)
    mgr._cache["bilibili"] = "old"          # noqa: SLF001 测内部缓存清理
    mgr._failed["douyin"] = "旧失败"        # noqa: SLF001

    mgr.reload("bilibili")
    assert "bilibili" not in mgr._cache     # noqa: SLF001
    assert mgr._failed.get("douyin")        # noqa: SLF001 别的平台不受影响

    mgr.reload()
    assert not mgr._cache and not mgr._failed   # noqa: SLF001


def test_header_filters_by_domain():
    cookies = [
        {"name": "SESSDATA", "value": "v1", "domain": ".bilibili.com"},
        {"name": "other", "value": "v2", "domain": ".example.com"},
    ]
    header = login_window._header_from_cookies(cookies, "www.bilibili.com")
    assert header == "SESSDATA=v1"


def test_marker_detection():
    host = "www.bilibili.com"
    anonymous = [{"name": "buvid3", "value": "x", "domain": ".bilibili.com"}]
    logged = anonymous + [{"name": "SESSDATA", "value": "s", "domain": ".bilibili.com"}]
    markers = login_window.SITES["bilibili"].markers
    assert login_window._has_marker(anonymous, host, markers) is False
    assert login_window._has_marker(logged, host, markers) is True


def test_unknown_platform_is_rejected():
    assert login_window.site_of("youtube") is None
    assert login_window.main(["--login", "youtube"]) == login_window.EXIT_CANCELLED
    assert login_window.main([]) == login_window.EXIT_CANCELLED


def test_every_site_has_login_page():
    for key, site in login_window.SITES.items():
        assert site.key == key
        assert site.url.startswith("https://")
        assert site.markers and site.window_title


def test_spawn_login_window_builds_command(monkeypatch):
    from dly.gui import server

    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(server.subprocess, "Popen", FakePopen)
    server._spawn_login_window("bilibili")          # noqa: SLF001

    cmd = captured["cmd"]
    assert "--login" in cmd
    assert cmd[-1] == "bilibili"
    # 源码模式走 python -m dly.gui，并带上 PYTHONPATH 让子进程能 import dly
    if any("python" in str(part).lower() for part in cmd[:1]):
        assert "dly.gui" in cmd
        assert captured["kwargs"]["env"]["PYTHONPATH"]


def test_simplecookie_objects_are_supported():
    """pywebview 6 的 WebView2 后端返回 SimpleCookie（每个只装一条）。"""
    from http.cookies import SimpleCookie

    c = SimpleCookie()
    c["SESSDATA"] = "sess-value"
    c["SESSDATA"]["domain"] = ".bilibili.com"

    other = SimpleCookie()
    other["x"] = "y"
    other["x"]["domain"] = ".example.com"

    host = "www.bilibili.com"
    assert login_window._header_from_cookies([c, other], host) == "SESSDATA=sess-value"
    assert login_window._has_marker([c], host, ("SESSDATA",)) is True
    assert login_window._has_marker([other], host, ("SESSDATA",)) is False
