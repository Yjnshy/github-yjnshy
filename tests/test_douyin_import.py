"""抖音喜欢/收藏批量导入的离线单元测试（不启动浏览器）。"""

from __future__ import annotations

from dly.core import douyin_import as di


def test_video_url():
    assert di.video_url("7709145837255518440") == (
        "https://www.douyin.com/video/7709145837255518440"
    )


def test_find_items_extracts_summary():
    out = {}
    di._find_items({
        "aweme_list": [{
            "aweme_id": "123",
            "desc": "标题",
            "author": {"nickname": "作者"},
            "video": {"cover": {"url_list": ["http://cover"]}},
        }]
    }, out)
    assert out == {
        "123": {"id": "123", "desc": "标题", "author": "作者", "cover": "http://cover"}
    }


def test_find_items_dedups_and_handles_nested():
    out = {}
    di._find_items({"a": {"aweme_id": 1, "desc": "x"}}, out)
    di._find_items([{"aweme_id": 1, "desc": "重复"}, {"aweme_id": 2}], out)
    assert set(out) == {"1", "2"}
    assert out["1"]["desc"] == "x"   # 先到的保留


def test_find_items_ignores_junk():
    out = {}
    di._find_items({"aweme_id": "not-a-number", "x": None}, out)
    di._find_items("plain string", out)
    assert out == {}


def test_api_tab_mapping():
    assert di._API_TABS["aweme/favorite"] == "like"
    assert di._API_TABS["aweme/listcollection"] == "favorite"


def test_free_port():
    port = di._free_port()
    assert isinstance(port, int) and 1024 < port < 65536


def test_decode_body_plain_and_base64():
    import base64
    assert di._decode_body({"body": '{"a":1}', "base64Encoded": False}) == '{"a":1}'
    raw = base64.b64encode("你好".encode()).decode()
    assert di._decode_body({"body": raw, "base64Encoded": True}) == "你好"


def test_login_cookies_defined():
    # 抖音 Web 登录态的关键项，用来判定专用 profile 是否已登录
    assert "sessionid" in di._LOGIN_COOKIES
    assert "sid_tt" in di._LOGIN_COOKIES


def test_close_gracefully_flushes_cookies():
    """扫码成功后若强杀 Chrome，Cookie 来不及落盘，下次扫描会误判未登录。"""
    from pathlib import Path
    from unittest.mock import Mock

    s = di.ChromeSession(Path("/tmp/profile"), Path("/chrome"))
    cdp, proc = Mock(), Mock()
    proc.poll.return_value = None
    proc.wait.side_effect = lambda timeout=None: setattr(proc, "poll", lambda: 0)
    s.cdp, s.proc = cdp, proc
    s.close()
    cdp.notify.assert_called_once_with("Browser.close")
    assert proc.wait.call_count == 1
    proc.terminate.assert_not_called()
    cdp.close.assert_called_once_with()
    assert s.proc is None


def test_cdp_notify_sends_command_without_waiting():
    import json
    from unittest.mock import Mock

    cdp = object.__new__(di.CDP)
    cdp._ws, cdp._seq = Mock(), 4
    cdp.notify("Browser.close", {"x": 1})
    payload = json.loads(cdp._ws.send.call_args.args[0])
    assert payload == {"id": 5, "method": "Browser.close", "params": {"x": 1}}
    assert cdp._seq == 5
