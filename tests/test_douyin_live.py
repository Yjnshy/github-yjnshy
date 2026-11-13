"""抖音直播解析的离线单元测试（不发网络请求）。"""

from __future__ import annotations

import json

import pytest

from media_toolkit.platforms.douyin import DouyinAdapter


def _room_with_origin(codec: str = "h264") -> dict:
    stream_data = json.dumps({
        "data": {
            "origin": {
                "main": {
                    "flv": "https://cdn.example/origin.flv?sig=1",
                    "hls": "https://cdn.example/origin.m3u8?sig=1",
                    "sdk_params": json.dumps({"VCodec": codec}),
                }
            }
        }
    })
    return {
        "status": 2,
        "title": "测试直播",
        "stream_url": {
            "live_core_sdk_data": {"pull_data": {"stream_data": stream_data}},
            "flv_pull_url": {
                "FULL_HD1": "https://cdn.example/full_hd.flv",
                "HD1": "https://cdn.example/hd.flv",
            },
            "hls_pull_url_map": {"FULL_HD1": "https://cdn.example/full_hd.m3u8"},
        },
    }


# ---------------------------------------------------------------- 房间号解析

@pytest.mark.parametrize("url, expected", [
    ("https://live.douyin.com/369324308707", "369324308707"),
    ("https://live.douyin.com/123456789?enter_from=x", "123456789"),
    ("https://v.douyin.com/iABCdef/", ""),
    ("https://live.douyin.com/", ""),
])
def test_extract_web_rid(url, expected):
    assert DouyinAdapter._extract_web_rid(url) == expected


# ---------------------------------------------------------------- 流地址挑选

def test_pick_stream_prefers_origin_flv_with_codec():
    url = DouyinAdapter._pick_stream_url(_room_with_origin())
    assert url.startswith("https://cdn.example/origin.flv")
    assert url.endswith("&codec=h264")          # 原画必须带 codec 参数


def test_pick_stream_falls_back_to_quality_map():
    room = {
        "stream_url": {
            "flv_pull_url": {"FULL_HD1": "https://cdn.example/full.flv"},
        }
    }
    assert DouyinAdapter._pick_stream_url(room) == "https://cdn.example/full.flv"


def test_pick_stream_falls_back_to_hls():
    room = {"stream_url": {"hls_pull_url_map": {"HD1": "https://cdn.example/a.m3u8"}}}
    assert DouyinAdapter._pick_stream_url(room) == "https://cdn.example/a.m3u8"


def test_pick_stream_returns_empty_when_offline():
    assert DouyinAdapter._pick_stream_url({"stream_url": {}}) == ""
    assert DouyinAdapter._pick_stream_url({}) == ""


def test_pick_stream_ignores_broken_stream_data():
    """stream_data 不是合法 JSON 时不能崩，应回落到档位地址。"""
    room = {
        "stream_url": {
            "live_core_sdk_data": {"pull_data": {"stream_data": "not-json"}},
            "flv_pull_url": {"SD1": "https://cdn.example/sd.flv"},
        }
    }
    assert DouyinAdapter._pick_stream_url(room) == "https://cdn.example/sd.flv"
