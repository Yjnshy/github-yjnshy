"""录制页三项新功能的回归测试：时长单位、自动续录、直播间标题预览。

界面改动在单页 HTML 里，这里覆盖的是后端契约：
- 前端传「数值 + 单位」，后端换算成 ffmpeg 用的秒数
- 自动续录循环按段推进、能被停止、连续失败会收手
- 链接探测接口回填标题所需的字段
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from dly.gui import server as srv
from dly.platforms.base import LiveInfo
from dly.recorder.recorder import LiveRecorder, RecordingError, _fmt_seconds


# ---------------------------------------------------------------- 时长单位

@pytest.mark.parametrize("payload, expected", [
    ({"segment_value": 30, "segment_unit": "s"}, 30.0),
    ({"segment_value": 30, "segment_unit": "min"}, 1800.0),
    ({"segment_value": 1.5, "segment_unit": "h"}, 5400.0),
    ({"segment_value": 0, "segment_unit": "s"}, 0.0),        # 0 = 不分段
    ({"segment_value": 0.1, "segment_unit": "s"}, 1.0),       # 下限 1 秒
    ({"segment_value": 99999, "segment_unit": "h"}, 86400.0),  # 上限 24 小时
    ({"segment_value": "abc", "segment_unit": "min"}, 0.0),
    ({"segment_value": float("nan"), "segment_unit": "s"}, 0.0),
    ({"segment": 30}, 1800.0),                                # 旧前端：分钟
    ({"segment": 0}, 0.0),
    ({}, 0.0),
    ({"segment_unit": "unknown", "segment_value": 2}, 120.0),  # 未知单位按分钟
])
def test_parse_segment_seconds(payload, expected):
    assert srv.parse_segment_seconds(payload) == expected


def test_fmt_seconds_has_no_trailing_dot_zero():
    assert _fmt_seconds(1800.0) == "1800"
    assert _fmt_seconds(0.5) == "0.5"
    assert _fmt_seconds(30.0) == "30"


def test_recorder_accepts_seconds(tmp_path):
    rec = LiveRecorder("ffmpeg", tmp_path, segment_seconds=45)
    cmd = rec.build_cmd(_live(), tmp_path / "a.ts")
    joined = " ".join(cmd)

    assert rec.segment_seconds == 45
    assert "-segment_time 45" in joined
    assert "-f segment" in joined
    assert rec.segment_minutes == pytest.approx(0.75)


def test_recorder_minutes_still_works(tmp_path):
    """CLI / 配置 / 值守仍按分钟传，不能破坏。"""
    rec = LiveRecorder("ffmpeg", tmp_path, segment_minutes=30)
    joined = " ".join(rec.build_cmd(_live(), tmp_path / "a.ts"))

    assert "-segment_time 1800" in joined
    assert "n_forced*600" not in joined       # 未转码时不该加强制关键帧


def test_recorder_seconds_overrides_minutes(tmp_path):
    rec = LiveRecorder("ffmpeg", tmp_path, segment_minutes=30,
                       segment_seconds=0)
    assert rec.segment_seconds == 0
    assert "-f segment" not in " ".join(rec.build_cmd(_live(), tmp_path / "a.ts"))


def test_recorder_fractional_minutes(tmp_path):
    rec = LiveRecorder("ffmpeg", tmp_path, fps=25, segment_minutes=0.5)
    joined = " ".join(rec.build_cmd(_live(), tmp_path / "a.ts"))

    assert "-segment_time 30" in joined
    assert "n_forced*30" in joined


def test_stop_requested_is_public():
    rec = LiveRecorder("ffmpeg", Path("."))
    assert rec.stop_requested is False
    rec.stop()
    assert rec.stop_requested is True


# ---------------------------------------------------------------- 自动续录

class _FakeAdapter:
    """按调用次数返回直播状态：前 n 次开播，之后下播。"""

    def __init__(self, live_rounds=99):
        self.live_rounds = live_rounds
        self.calls = 0

    def get_live_info(self, url):
        self.calls += 1
        live = _live()
        if self.calls > self.live_rounds:
            live.is_live = False
            live.stream_url = ""
        return live


class _FakeRecorder:
    instances = []
    fail = False                 # 类属性：测试可整体打开「模拟断流」

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.parts = []
        self.stop_requested = False
        _FakeRecorder.instances.append(self)

    def record_once(self, live, part=0):
        self.parts.append(part)
        if self.fail:
            raise RecordingError("模拟断流", detail="fake stderr")
        return Path("fake_part.ts")

    def stop(self):
        self.stop_requested = True


@pytest.fixture
def fake_record_env(monkeypatch, tmp_path):
    """把网络、cookie、ffmpeg、真实录制器全部替换掉，只跑循环逻辑。"""
    _FakeRecorder.instances = []
    monkeypatch.setattr(srv, "load_config", lambda: SimpleNamespace(
        cookies=None, proxy="", ffmpeg_path="",
        output_dir=str(tmp_path), record=SimpleNamespace(format="ts")))
    monkeypatch.setattr(srv, "find_ffmpeg", lambda *_: "ffmpeg")
    monkeypatch.setattr("dly.cookies.manager.CookieManager",
                        lambda *a, **k: object())
    monkeypatch.setattr("dly.recorder.recorder.LiveRecorder", _FakeRecorder)
    return {}


def _state():
    return srv.AppState()


def test_auto_next_records_another_part(fake_record_env, monkeypatch):
    adapter = _FakeAdapter(live_rounds=2)   # 首次 + 一次续录探测，随后下播
    monkeypatch.setattr("dly.platforms.find_adapter", lambda *a, **k: adapter)
    state = _state()

    srv._run_record(state, "https://live.douyin.com/123", "", 30,
                    auto_next=True)

    rec = _FakeRecorder.instances[0]
    assert rec.parts == [0, 2]              # 第二段用 _part02，不覆盖第一段
    assert rec.kwargs["segment_seconds"] == 30
    assert state.record["active"] is False
    assert state.record["status"] == "已停止"
    texts = [e["text"] for e in state.events_since(0)]
    assert any("自动续录：开始第 2 段" in t for t in texts)


def test_auto_next_off_stops_after_one_part(fake_record_env, monkeypatch):
    monkeypatch.setattr("dly.platforms.find_adapter",
                        lambda *a, **k: _FakeAdapter())
    state = _state()

    srv._run_record(state, "https://live.douyin.com/123", "", 0,
                    auto_next=False)

    assert _FakeRecorder.instances[0].parts == [0]
    assert state.record["status"] == "已完成"
    assert state.record_stop.is_set() is False   # finally 里已复位


def test_stop_signal_breaks_between_parts(fake_record_env, monkeypatch):
    monkeypatch.setattr("dly.platforms.find_adapter",
                        lambda *a, **k: _FakeAdapter())

    original = _FakeRecorder.record_once

    def stop_after_first(self, live, part=0):
        result = original(self, live, part)
        self.stop_requested = True          # 模拟用户点了「停止录制」
        return result

    monkeypatch.setattr(_FakeRecorder, "record_once", stop_after_first)
    state = _state()
    srv._run_record(state, "https://live.douyin.com/123", "", 0,
                    auto_next=True)

    assert _FakeRecorder.instances[0].parts == [0]
    assert state.record["status"] == "已停止"


def test_continuous_failures_give_up(fake_record_env, monkeypatch):
    monkeypatch.setattr("dly.platforms.find_adapter",
                        lambda *a, **k: _FakeAdapter())
    _FakeRecorder.fail = True
    try:
        state = _state()
        srv._run_record(state, "https://live.douyin.com/123", "", 0,
                        auto_next=True)
    finally:
        _FakeRecorder.fail = False

    assert len(_FakeRecorder.instances[0].parts) == srv.MAX_CONTINUOUS_FAILURES
    assert state.record["status"] == "录制失败"


def test_unknown_url_never_starts_recorder(fake_record_env, monkeypatch):
    monkeypatch.setattr("dly.platforms.find_adapter", lambda *a, **k: None)
    state = _state()
    srv._run_record(state, "https://example.com/nope", "", 0)

    assert _FakeRecorder.instances == []
    assert state.record["status"] == "无法识别链接"
    assert state.record["active"] is False


# ---------------------------------------------------------------- 标题预览

def test_probe_live_rejects_bad_url():
    assert srv.probe_live("not-a-url")["ok"] is False
    assert srv.probe_live("ftp://live.douyin.com/123")["ok"] is False


def test_probe_live_returns_title(monkeypatch):
    monkeypatch.setattr(srv, "load_config", lambda: SimpleNamespace(
        cookies=None, proxy=""))
    monkeypatch.setattr("dly.cookies.manager.CookieManager",
                        lambda *a, **k: object())
    monkeypatch.setattr("dly.platforms.find_adapter",
                        lambda *a, **k: _FakeAdapter())

    result = srv.probe_live("https://live.douyin.com/123")
    assert result["ok"] is True
    assert result["title"] == "今晚聊天"
    assert result["anchor"] == "测试主播"
    assert result["is_live"] is True
    assert result["has_stream"] is True


def test_probe_live_handles_adapter_failure(monkeypatch):
    class Broken:
        def get_live_info(self, url):
            return None

    monkeypatch.setattr(srv, "load_config", lambda: SimpleNamespace(
        cookies=None, proxy=""))
    monkeypatch.setattr("dly.cookies.manager.CookieManager",
                        lambda *a, **k: object())
    monkeypatch.setattr("dly.platforms.find_adapter", lambda *a, **k: Broken())
    assert srv.probe_live("https://live.douyin.com/123")["ok"] is False


# ---------------------------------------------------------------- HTTP 接口

@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(srv, "DEFAULT_PORT", 8913)
    ctx = srv.create_app(desktop=True)
    ctx.start()
    try:
        yield ctx
    finally:
        ctx.shutdown()


def _post(base: str, path: str, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def test_probe_endpoint_passes_through_result(app, monkeypatch):
    monkeypatch.setattr(srv, "probe_live",
                        lambda url: {"ok": True, "title": "房间标题",
                                     "anchor": "主播", "is_live": True,
                                     "platform": "douyin", "has_stream": True})
    status, body = _post(app.url, "/api/record/probe",
                         {"url": "https://live.douyin.com/123"})
    assert status == 200
    assert body["title"] == "房间标题"


def test_probe_endpoint_swallows_backend_error(app, monkeypatch):
    def boom(_url):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(srv, "probe_live", boom)
    status, body = _post(app.url, "/api/record/probe", {"url": "https://x"})
    assert status == 200
    assert body["ok"] is False
    assert "网络炸了" in body["error"]


def test_start_converts_unit_before_launching(app, monkeypatch):
    seen = {}
    done = threading.Event()

    def fake_run(state, url, outroot, segment=0, fps=0, auto_next=True):
        seen.update(url=url, segment=segment, auto_next=auto_next)
        done.set()

    monkeypatch.setattr(srv, "_run_record", fake_run)
    status, body = _post(app.url, "/api/record/start", {
        "url": "https://live.douyin.com/123",
        "segment_value": 45,
        "segment_unit": "s",
        "auto_next": False,
    })
    assert status == 200 and body == {"ok": True}
    assert done.wait(2), "录制线程没启动"
    assert seen == {"url": "https://live.douyin.com/123",
                    "segment": 45.0, "auto_next": False}


def test_start_legacy_segment_field_still_works(app, monkeypatch):
    seen = {}
    done = threading.Event()

    def fake_run(state, url, outroot, segment=0, fps=0, auto_next=True):
        seen.update(segment=segment, auto_next=auto_next)
        done.set()

    monkeypatch.setattr(srv, "_run_record", fake_run)
    _post(app.url, "/api/record/start",
          {"url": "https://live.douyin.com/123", "segment": 10})
    assert done.wait(2)
    assert seen == {"segment": 600.0, "auto_next": True}


def _live() -> LiveInfo:
    return LiveInfo(
        platform="douyin",
        room_url="https://live.douyin.com/123",
        is_live=True,
        title="今晚聊天",
        anchor="测试主播",
        stream_url="https://cdn.example.com/live.flv",
        referer="https://live.douyin.com/123",
    )
