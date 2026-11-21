"""汽水音乐免登录采集：分享文本识别、页面解析、下载与入库。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dly.core import qishui_import as qi
from dly.core import paths
from dly.core.library import Library


def test_parse_share_recognizes_song_playlist_and_id():
    song = "《小半》@汽水音乐 https://www.douyin.com/qishui/song/7079108541549643812"
    assert qi.parse_share(song) == {"kind": "track", "value": "7079108541549643812"}
    assert qi.parse_share("7079108541549643812")["kind"] == "track"

    pl = qi.parse_share("复制此链接 https://qishui.douyin.com/s/iQgxdHx2/ 打开汽水音乐")
    assert pl["kind"] == "short"
    assert pl["value"].startswith("https://qishui.douyin.com/s/")

    # 短链本身分不出单曲/歌单，解析函数都返回 short
    song_short = qi.parse_share("《四季予你》@汽水音乐 https://qishui.douyin.com/s/i41FQxYa/")
    assert song_short["kind"] == "short"

    direct = qi.parse_share("https://music.douyin.com/qishui/share/playlist?playlist_id=7096700219496368135")
    assert direct == {"kind": "playlist", "value": "7096700219496368135"}

    with pytest.raises(ValueError):
        qi.parse_share("随便一段文字")


def test_router_data_extracts_balanced_json():
    html = '<script>var _ROUTER_DATA = {"loaderData":{"song_(id)/page":{"track_id":"123"}}};</script>'
    data = qi._router_data(html)
    assert data["loaderData"]["song_(id)/page"]["track_id"] == "123"


def test_track_from_option_extracts_fields():
    opt = {
        "track_id": "123",
        "trackName": "测试歌",
        "artistName": "测试歌手",
        "coverURL": "https://p3-luna.douyinpic.com/img/cover.jpg",
        "url": "https://v26-luna.douyinvod.com/a.m4a",
        "duration": 123.4,
        "lyrics": {"sentences": [{"text": "第一句"}, {"text": "第二句"}]},
        "trackInfo": {"album": {"name": "测试专辑"}},
    }
    t = qi._track_from_option(opt, "https://www.douyin.com/qishui/song/123")
    assert t.id == "123"
    assert t.title == "测试歌"
    assert t.artist == "测试歌手"
    assert t.album == "测试专辑"
    assert t.audio_url.endswith(".m4a")
    assert t.cover_url.endswith(".jpg")
    assert t.lyric == "第一句\n第二句"
    assert t.preview is True


def test_http_rejects_foreign_host():
    with pytest.raises(ValueError, match="不是汽水/抖音域名"):
        qi._http("https://example.com/whatever")


def test_library_set_cover(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    lib = Library()
    audio = tmp_path / "song.m4a"
    audio.write_bytes(b"\x00\x00\x00\x1cftypM4A ")
    track = lib.add(audio, title="歌")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xd9")
    assert lib.set_cover(track["id"], cover) is True
    row = lib.get(track["id"])
    assert Path(row["cover"]).read_bytes() == cover.read_bytes()
    assert lib.public()[0]["has_cover"] is True
