"""录制器单元测试：命令构造、请求头透传、失败校验。

全部离线运行：不拉流、不联网，用假的 Popen 模拟 ffmpeg 行为。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from dly.platforms.base import UA_PC, LiveInfo
from dly.recorder import recorder as recorder_mod
from dly.recorder.recorder import (
    MAX_COOKIE_HEADER, MIN_OUTPUT_BYTES, LiveRecorder, RecordingError,
    _microseconds_to_seconds, _trim_cookie,
)


def _live(**kwargs) -> LiveInfo:
    base = dict(
        platform="douyin",
        room_url="https://live.douyin.com/123456",
        is_live=True,
        title="测试直播",
        anchor="测试主播",
        stream_url="https://example.com/live.flv",
        referer="https://live.douyin.com/123456",
        user_agent=UA_PC,
        cookie="sessionid=abc; ttwid=def",
    )
    base.update(kwargs)
    return LiveInfo(**base)


def _cmd_of(recorder: LiveRecorder, live: LiveInfo, outfile: Path) -> str:
    return " ".join(recorder.build_cmd(live, outfile))


# ---------------------------------------------------------------- 命令构造

def test_build_cmd_passes_stream_context(tmp_path):
    rec = LiveRecorder("ffmpeg", tmp_path)
    cmd = rec.build_cmd(_live(), tmp_path / "a.ts")
    joined = " ".join(cmd)

    assert cmd[0] == "ffmpeg"
    assert "-c copy" in joined            # 纯转封装，不重编码
    assert UA_PC in joined                # 抖音 CDN 校验 UA
    assert "-reconnect 1" in joined       # 断流自动重连
    assert cmd[-1] == str(tmp_path / "a.ts")


def test_build_cmd_headers_contain_referer_and_cookie(tmp_path):
    rec = LiveRecorder("ffmpeg", tmp_path)
    headers = rec._headers(_live())

    assert headers.endswith("\r\n")        # ffmpeg 要求 CRLF 结尾
    assert "Referer: https://live.douyin.com/123456" in headers
    assert "Cookie: sessionid=abc; ttwid=def" in headers


def test_headers_omit_cookie_when_empty(tmp_path):
    rec = LiveRecorder("ffmpeg", tmp_path)
    headers = rec._headers(_live(cookie=""))

    assert "Cookie:" not in headers
    assert headers.endswith("\r\n")


def test_build_cmd_segment_mode(tmp_path):
    rec = LiveRecorder("ffmpeg", tmp_path, fmt="ts", segment_minutes=30)
    cmd = rec.build_cmd(_live(), tmp_path / "a.ts")
    joined = " ".join(cmd)

    assert "-f segment" in joined
    assert "-segment_time 1800" in joined          # 30 分钟 = 1800 秒
    assert "-segment_format mpegts" in joined      # 分段 muxer 必须显式指定
    assert cmd[-1].endswith("a_%03d.ts")


def test_progress_flag_present(tmp_path):
    rec = LiveRecorder("ffmpeg", tmp_path)
    cmd = rec.build_cmd(_live(), tmp_path / "a.ts")
    assert "-progress" in cmd and "pipe:1" in cmd


# ---------------------------------------------------------------- 帧率

def test_default_fps_uses_stream_copy(tmp_path):
    rec = LiveRecorder("ffmpeg", tmp_path)
    assert "-c copy" in " ".join(rec.build_cmd(_live(), tmp_path / "a.ts"))


def test_fps_switches_to_transcode(tmp_path):
    """改帧率只能重新编码，不能再用 -c copy。"""
    rec = LiveRecorder("ffmpeg", tmp_path, fps=30)
    joined = " ".join(rec.build_cmd(_live(), tmp_path / "a.ts"))

    assert "-c:v libx264" in joined
    assert "-r 30" in joined
    assert "-c copy" not in joined


def test_invalid_fps_falls_back_to_copy(tmp_path):
    rec = LiveRecorder("ffmpeg", tmp_path, fps=999)
    assert rec.fps == 0
    assert "-c copy" in " ".join(rec.build_cmd(_live(), tmp_path / "a.ts"))


def test_fps_segment_forces_keyframes(tmp_path):
    """转码分段必须强制关键帧，否则段边界会漂移到下一个自然关键帧。"""
    rec = LiveRecorder("ffmpeg", tmp_path, fps=25, segment_minutes=10)
    joined = " ".join(rec.build_cmd(_live(), tmp_path / "a.ts"))

    assert "-force_key_frames" in joined
    assert "n_forced*600" in joined        # 10 分钟 = 600 秒


# ---------------------------------------------------------------- Cookie 裁剪

def test_short_cookie_untouched():
    cookie = "ttwid=aaa; sessionid=bbb"
    assert _trim_cookie(cookie) == cookie


def test_long_cookie_trimmed_under_limit():
    """完整 Cookie 7~8KB 会让 ffmpeg 报 overlong headers，必须裁到限制内。"""
    cookie = "; ".join(f"k{i}=v{'x' * 40}" for i in range(300))
    cookie += "; ttwid=keepme; msToken=tok"
    trimmed = _trim_cookie(cookie)

    assert len(trimmed) <= MAX_COOKIE_HEADER
    assert "ttwid=keepme" in trimmed
    assert "msToken=tok" in trimmed


def test_headers_use_trimmed_cookie(tmp_path):
    rec = LiveRecorder("ffmpeg", tmp_path)
    live = _live(cookie="; ".join(f"k{i}=v{'x' * 40}" for i in range(300))
                 + "; ttwid=keepme")
    headers = rec._headers(live)

    assert len(headers) < MAX_COOKIE_HEADER + 200
    assert "ttwid=keepme" in headers


# ---------------------------------------------------------------- 小工具

@pytest.mark.parametrize("raw, expected", [
    ("12500000", 12.5),
    ("0", 0.0),
    ("N/A", 0.0),
    ("", 0.0),
])
def test_microseconds_to_seconds(raw, expected):
    assert _microseconds_to_seconds(raw) == expected


def test_collect_outputs_segment_glob(tmp_path):
    (tmp_path / "a_000.ts").write_bytes(b"x")
    (tmp_path / "a_001.ts").write_bytes(b"x")
    (tmp_path / "other.ts").write_bytes(b"x")

    rec = LiveRecorder("ffmpeg", tmp_path, segment_minutes=10)
    found = rec._collect_outputs("a")
    assert [p.name for p in found] == ["a_000.ts", "a_001.ts"]


# ---------------------------------------------------------------- 失败处理

class _FakeProc:
    """假 ffmpeg 进程：可以指定是否产出文件、stderr 内容、退出码。"""

    def __init__(self, cmd, produce_bytes=0, stderr="", returncode=1, **_):
        self._cmd = cmd
        self.returncode = returncode
        self.stdout = io.StringIO("out_time_us=1000000\ntotal_size=1024\nprogress=end\n")
        self.stderr = io.StringIO(stderr)
        self.pid = 999_999
        if produce_bytes:
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_bytes(b"0" * produce_bytes)

    def wait(self, *args, **kwargs):
        return self.returncode

    def poll(self):
        return self.returncode


def test_record_raises_when_no_data(tmp_path, monkeypatch):
    """ffmpeg 403 秒退：必须报错并清掉 0 字节文件，而不是假装成功。"""
    monkeypatch.setattr(
        recorder_mod.subprocess, "Popen",
        lambda cmd, **kw: _FakeProc(
            cmd, produce_bytes=0, returncode=1,
            stderr="Error opening input files: Server returned 403 Forbidden\n"),
    )
    rec = LiveRecorder("ffmpeg", tmp_path)

    with pytest.raises(RecordingError) as exc_info:
        rec.record_once(_live())

    assert "403 Forbidden" in exc_info.value.detail
    assert not list(tmp_path.glob("*.ts"))     # 失败产物已清理


def test_record_succeeds_with_data(tmp_path, monkeypatch):
    monkeypatch.setattr(
        recorder_mod.subprocess, "Popen",
        lambda cmd, **kw: _FakeProc(cmd, produce_bytes=MIN_OUTPUT_BYTES * 2),
    )
    rec = LiveRecorder("ffmpeg", tmp_path)

    out = rec.record_once(_live())

    assert out.is_file()
    assert out.stat().st_size >= MIN_OUTPUT_BYTES


def test_record_without_stream_url(tmp_path):
    rec = LiveRecorder("ffmpeg", tmp_path)
    with pytest.raises(RecordingError, match="流地址为空"):
        rec.record_once(_live(stream_url=""))
