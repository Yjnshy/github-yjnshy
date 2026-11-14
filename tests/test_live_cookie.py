"""抖音直播链路不得携带登录凭证。

带着浏览器里的 sessionid 等登录 Cookie 去请求直播间接口 / 拉流，会被服务端
判定为「同一账号在别处登录」，把用户浏览器或手机上的会话挤下线。
直播间信息与流地址用匿名风控 Cookie 即可取到，因此：
- 直播探测（get_live_info）与 ffmpeg 拉流头都要剔除登录凭证
- 视频无水印解析（resolve）确实需要登录态，保持完整 Cookie 不变
- 匿名策略只作用于抖音，不影响其他平台
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dly.platforms.base import LiveInfo, strip_login_cookies
from dly.platforms.douyin import DouyinAdapter
from dly.recorder.recorder import LiveRecorder, _trim_cookie

FULL = ("ttwid=anon; msToken=tok; s_v_web_id=web; odin_tt=dev; "
        "passport_csrf_token=CSRF; "
        "sessionid=SECRET; sessionid_ss=SECRET2; sid_tt=S; sid_guard=G; "
        "uid_tt=U; uid_tt_ss=U2; ssid_ucp_v1=V; passport_auth_status=P; "
        "LOGIN_STATUS=1; d_ticket=D")

ANON = ("ttwid", "msToken", "s_v_web_id", "odin_tt", "passport_csrf_token")
LOGIN = ("sessionid", "sid_tt", "sid_guard", "uid_tt", "ssid_ucp_v1",
         "passport_auth_status", "login_status", "d_ticket")


def _adapter(header: str = FULL) -> DouyinAdapter:
    return DouyinAdapter(SimpleNamespace(get_header=lambda platform: header))


# ---------------------------------------------------------------- 纯函数

def test_strip_keeps_anonymous_and_drops_login_fields():
    out = strip_login_cookies(FULL)
    for key in ANON:
        assert f"{key}=" in out
    for key in LOGIN:
        assert key not in out.lower()
    assert "SECRET" not in out


@pytest.mark.parametrize("header, expected", [
    ("", ""),
    (None, ""),
    ("ttwid=x", "ttwid=x"),
    ("ttwid=x;  sessionid=y ; badpart; msToken=z", "ttwid=x; msToken=z"),
    ("  sessionid=only  ", ""),
    ("LOGIN_STATUS=1; passport_csrf_token=csrf", "passport_csrf_token=csrf"),
])
def test_strip_edge_cases(header, expected):
    assert strip_login_cookies(header or "") == expected


def test_strip_is_case_insensitive():
    out = strip_login_cookies("SESSIONID=a; SID_GUARD=b; ttwid=c")
    assert out == "ttwid=c"


# ---------------------------------------------------------------- 直播探测

def test_get_live_info_uses_anonymous_cookie():
    """房间接口与交给 ffmpeg 的 LiveInfo 都不能带登录凭证。"""
    adapter = _adapter()
    captured = {}

    def fake_fetch(rid, referer, cookie):
        captured["cookie"] = cookie
        return {
            "data": [{
                "status": 2, "title": "测试直播",
                "stream_url": {"flv_pull_url":
                               {"FULL_HD1": "https://cdn.example/live.flv"}},
            }],
            "user": {"nickname": "测试主播"},
        }

    with patch.object(adapter, "_fetch_room_api", side_effect=fake_fetch):
        live = adapter.get_live_info("https://live.douyin.com/123456789")

    assert live.is_live and live.title == "测试直播"
    assert "ttwid=anon" in captured["cookie"]
    assert any(k in captured["cookie"].lower() for k in LOGIN) is False
    assert live.cookie == captured["cookie"]
    assert "sessionid" not in live.cookie.lower()


def test_client_omits_cookie_header_when_empty():
    client = _adapter()._client("")
    try:
        assert "cookie" not in {k.lower() for k in client.headers}
    finally:
        client.close()


def test_resolve_still_uses_full_cookie():
    """视频下载解析需要登录态，不能被直播的匿名策略误伤。"""
    adapter = _adapter()
    captured = {}

    def fake_follow(url, cookie):
        captured["cookie"] = cookie
        return "https://www.douyin.com/video/1234567890123456789"

    def fake_router(item_id, cookie):
        return {"item_list": [{
            "desc": "作品标题",
            "video": {"play_addr": {"url_list": ["https://cdn/x/playwm"]}},
        }]}

    with patch.object(adapter, "_follow_redirect", side_effect=fake_follow), \
            patch.object(adapter, "_fetch_router_data", side_effect=fake_router):
        info = adapter.resolve("https://v.douyin.com/abc/")

    assert info is not None and info.title == "作品标题"
    assert "sessionid=SECRET" in captured["cookie"]


# ---------------------------------------------------------------- 拉流请求头

def test_ffmpeg_headers_never_carry_login_cookie():
    live = LiveInfo(platform="douyin",
                    room_url="https://live.douyin.com/123456",
                    is_live=True, cookie=FULL,
                    referer="https://live.douyin.com/123456")
    headers = LiveRecorder._headers(live)

    assert "Cookie:" in headers
    assert "ttwid=anon" in headers
    assert "sessionid" not in headers.lower()


def test_short_douyin_cookie_also_stripped():
    """Cookie 短于长度上限时同样要剥离登录凭证，不能走原样返回的捷径。"""
    out = _trim_cookie("ttwid=x; sessionid=SECRET", platform="douyin")
    assert out == "ttwid=x"


def test_other_platform_cookie_unchanged():
    """匿名策略只作用于抖音，其他平台保持原有行为。"""
    cookie = "sessionid=abc; ttwid=def"
    assert _trim_cookie(cookie, platform="bilibili") == cookie
