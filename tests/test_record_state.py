"""录制状态展示回归：默认只录一段、到点可感知、墙钟计时不跳秒。

用户实测：填 10 秒、自认为没勾自动续录，界面却一直录下去；同时「已录时长」
偶尔一次跳两秒。前者是复选框默认勾选 + 状态卡看不出已经停过一段，后者是
直接拿 ffmpeg 的 ``out_time_us``（跟着直播流时间戳走）当显示值。
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from dly.gui import server as srv
from dly.platforms.base import LiveInfo

HTML = (Path(__file__).resolve().parents[1]
        / "src/dly/gui/assets/index.html")


def html_text() -> str:
    return HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 前端默认值

def test_auto_next_is_unchecked_in_markup():
    """HTML 里不能带 checked：用户「没去勾」就该只录一段。"""
    text = html_text()
    assert 'id="rec-auto-next">' in text
    assert 'id="rec-auto-next" checked' not in text


def test_reload_resets_checkbox_and_polls_fast():
    """浏览器刷新会恢复表单勾选状态，需要显式复位；轮询要够快才不跳秒。"""
    text = html_text()
    assert "$('rec-auto-next').checked = false" in text
    assert "setTimeout(poll, 300)" in text
    assert "setTimeout(poll, 900)" not in text


def test_render_uses_elapsed_and_explains_finish():
    text = html_text()
    assert "typeof rec.elapsed === 'number'" in text
    assert "已自动停止并保存" in text
    assert "第 ${rec.part} 段" in text


# ---------------------------------------------------------------- 墙钟计时

def test_elapsed_uses_wall_clock_not_ffmpeg_timestamp():
    state = srv.AppState()
    # ffmpeg 上报 1 秒（缓冲 / 时间戳跳变），但墙钟已经过了 3.2 秒
    state.set_record(active=True, seconds=1.0,
                     started_at=time.monotonic() - 3.2)
    rec = state.record_snapshot()

    assert rec["elapsed"] == pytest.approx(3.2, abs=0.2)
    assert rec["seconds"] == 1.0            # 内容时长仍保留
    assert "started_at" not in rec and "ended_at" not in rec


def test_elapsed_freezes_once_finished():
    state = srv.AppState()
    now = time.monotonic()
    state.set_record(active=False, seconds=9.9,
                     started_at=now - 10, ended_at=now)
    first = state.record_snapshot()["elapsed"]
    time.sleep(0.05)
    second = state.record_snapshot()["elapsed"]

    assert first == second
    assert first == pytest.approx(10.0, abs=0.2)


def test_elapsed_falls_back_to_seconds_without_start():
    state = srv.AppState()
    state.set_record(seconds=4.5)
    assert state.record_snapshot()["elapsed"] == 4.5


def test_elapsed_grows_monotonically_between_polls():
    state = srv.AppState()
    state.set_record(active=True, started_at=time.monotonic())
    a = state.record_snapshot()["elapsed"]
    time.sleep(0.3)
    b = state.record_snapshot()["elapsed"]
    assert 0.25 < b - a < 0.5          # 每次轮询只推进约 0.3 秒，不会跳 2 秒


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(srv, "DEFAULT_PORT", 8919)
    ctx = srv.create_app(desktop=True)
    ctx.start()
    try:
        yield ctx
    finally:
        ctx.shutdown()


def test_state_endpoint_exposes_elapsed(app):
    app.state.set_record(active=True, seconds=0.5,
                         started_at=time.monotonic() - 2.0)
    with urllib.request.urlopen(app.url + "api/state", timeout=5) as res:
        body = json.loads(res.read().decode("utf-8"))
    rec = body["snapshot"]["record"]

    assert rec["elapsed"] >= 1.9
    assert "started_at" not in rec
    assert rec["seconds"] == 0.5


# ---------------------------------------------------------------- 录制展示字段

class _FakeAdapter:
    def get_live_info(self, url):
        return LiveInfo(platform="douyin", room_url=url, is_live=True,
                        title="测试房间", anchor="测试主播",
                        stream_url="https://cdn.example.com/live.flv",
                        referer=url)


class _FakeRecorder:
    instances = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.stop_requested = False
        self.parts = []
        _FakeRecorder.instances.append(self)

    def record_once(self, live, part=0):
        self.parts.append(part)
        return Path("fake.ts")

    def stop(self):
        self.stop_requested = True


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    _FakeRecorder.instances = []
    monkeypatch.setattr(srv, "load_config", lambda: SimpleNamespace(
        cookies=None, proxy="", ffmpeg_path="", output_dir=str(tmp_path),
        record=SimpleNamespace(format="ts")))
    monkeypatch.setattr(srv, "find_ffmpeg", lambda *_: "ffmpeg")
    monkeypatch.setattr("dly.cookies.manager.CookieManager",
                        lambda *a, **k: object())
    monkeypatch.setattr("dly.platforms.find_adapter",
                        lambda *a, **k: _FakeAdapter())
    monkeypatch.setattr("dly.recorder.recorder.LiveRecorder", _FakeRecorder)
    return {}


def _run(state, seconds=10, auto_next=False):
    srv._run_record(state, "https://live.douyin.com/123", "", seconds,
                    auto_next=auto_next)


def test_single_part_state_is_explicit(fake_env):
    """填了 10 秒且没勾续录：状态卡能显示目标时长、段号和明确的完成态。"""
    state = srv.AppState()
    _run(state)

    assert state.record["status"] == "已完成"
    assert state.record["active"] is False
    assert state.record["part"] == 1
    assert state.record["duration"] == 10
    assert state.record["auto_next"] is False
    assert state.record["ended_at"] is not None
    assert state.record_snapshot()["elapsed"] >= 0
    texts = [e["text"] for e in state.events_since(0)]
    assert any("自动停止（未开启自动续录）" in t for t in texts)


def test_auto_next_flag_is_reflected_in_state(fake_env, monkeypatch):
    state = srv.AppState()
    # 续录开启但直播间随后下播，避免无限循环
    class _OneShot(_FakeAdapter):
        calls = 0

        def get_live_info(self, url):
            self.calls += 1
            live = super().get_live_info(url)
            if self.calls > 1:
                live.is_live = False
                live.stream_url = ""
            return live

    monkeypatch.setattr("dly.platforms.find_adapter",
                        lambda *a, **k: _OneShot())
    _run(state, auto_next=True)

    assert state.record["auto_next"] is True
    assert state.record["part"] >= 1
    assert state.record["active"] is False


def test_max_duration_still_goes_to_recorder(fake_env):
    """回归：时长交给 -t，不是永不退出的 -f segment。"""
    state = srv.AppState()
    _run(state, seconds=10)

    rec = _FakeRecorder.instances[0]
    assert rec.kwargs["max_duration"] == 10
    assert "segment_seconds" not in rec.kwargs   # 不能用无限分段 muxer
    assert rec.parts == [0]                     # 只录一段
    assert state.recorder is None                # finally 已清理
    texts = [e["text"] for e in state.events_since(0)]
    assert any("录制时长：10 秒" in t for t in texts)
    assert any("自动续录：关" in t for t in texts)
