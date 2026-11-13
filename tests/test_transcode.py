"""帧率转码单元测试：参数归一化、命令构造、容器过滤、真实转码。

真实转码部分用 ffmpeg 现合成的小视频验证；没装 ffmpeg 时自动跳过，
保证在无多媒体的环境里也能跑通测试。
"""

from __future__ import annotations

import subprocess

import pytest

from media_toolkit.core.ffmpeg import find_ffmpeg, find_ffprobe
from media_toolkit.core.transcode import (FPS_CHOICES, build_cmd, fps_label,
                                          is_transcodable, normalize_fps,
                                          probe_fps, transcode)


# ---------------------------------------------------------------- 参数归一化

@pytest.mark.parametrize("raw, expected", [
    (30, 30),
    ("25", 25),
    (45, 45),          # 下拉框之外的合法值也允许（配置文件可填任意值）
    (120, 120),        # 上界
    (0, 0),            # 0 = 不转码
    (None, 0),
    ("", 0),
    ("abc", 0),
    (-5, 0),
    (200, 0),          # 超出合理范围一律当作不转码
])
def test_normalize_fps(raw, expected):
    assert normalize_fps(raw) == expected


def test_fps_label():
    assert fps_label(0) == "原帧率"
    assert fps_label(30) == "30fps"


def test_choices_include_original():
    assert 0 in FPS_CHOICES


# ---------------------------------------------------------------- 容器过滤

@pytest.mark.parametrize("name, expected", [
    ("a.mp4", True),
    ("a.ts", True),
    ("a.mkv", True),
    ("a.flv", True),
    ("a.mp3", False),      # 纯音频没有帧率概念
    ("a.jpg", False),      # 图文作品
    ("a.txt", False),      # 文案
    ("a.webm", False),     # webm 装不下 H.264，直接跳过
])
def test_is_transcodable(tmp_path, name, expected):
    assert is_transcodable(tmp_path / name) is expected


# ---------------------------------------------------------------- 命令构造

def test_build_cmd_has_target_fps(tmp_path):
    cmd = build_cmd("ffmpeg", tmp_path / "a.mp4", tmp_path / "b.mp4", 30)
    joined = " ".join(cmd)

    assert cmd[0] == "ffmpeg"
    assert "-r 30" in joined
    assert "libx264" in joined
    assert "-c:v libx264" in joined
    assert "-movflags +faststart" in joined     # mp4 快进索引，边下边播更顺
    assert "-map 0:a:0?" in joined              # 无音频的直播也不报错
    assert cmd[-1] == str(tmp_path / "b.mp4")


def test_build_cmd_non_mp4_without_faststart(tmp_path):
    joined = " ".join(build_cmd("ffmpeg", "a.ts", "b.ts", 25))
    assert "+faststart" not in joined


def test_build_cmd_gop_for_segments(tmp_path):
    joined = " ".join(build_cmd("ffmpeg", "a.ts", "b.ts", 25,
                                gop_seconds=60))
    assert "-g 1500" in joined                  # 25fps × 60s


def test_transcode_skips_unsupported_container(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"0")
    assert transcode("ffmpeg", audio, 30) is None
    assert audio.is_file()                      # 原文件不能被动


def test_transcode_skips_when_fps_zero(tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"0")
    assert transcode("ffmpeg", video, 0) is None


# ---------------------------------------------------------------- 真实转码

@pytest.fixture(scope="module")
def ffmpeg_pair():
    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe(ffmpeg)
    if not ffmpeg or not ffprobe:
        pytest.skip("环境里没有 ffmpeg / ffprobe")
    return ffmpeg, ffprobe


@pytest.fixture
def sample_video(tmp_path, ffmpeg_pair):
    ffmpeg, _ = ffmpeg_pair
    src = tmp_path / "示例_作者_标题_1699999999.mp4"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(src)],
        check=True, capture_output=True,
    )
    return src


def test_probe_fps_reads_source(sample_video, ffmpeg_pair):
    _, ffprobe = ffmpeg_pair
    assert probe_fps(ffprobe, sample_video) == pytest.approx(30, abs=0.5)


def test_transcode_replaces_in_place(sample_video, ffmpeg_pair):
    """转码后文件名保持不变，也不留临时文件。"""
    ffmpeg, ffprobe = ffmpeg_pair
    out = transcode(ffmpeg, sample_video, 15, ffprobe_path=ffprobe)

    assert out == sample_video
    assert probe_fps(ffprobe, out) == pytest.approx(15, abs=0.5)
    assert not list(sample_video.parent.glob("*__fps_tmp*"))


def test_transcode_keep_original(sample_video, ffmpeg_pair):
    ffmpeg, ffprobe = ffmpeg_pair
    transcode(ffmpeg, sample_video, 24, keep_original=True,
              ffprobe_path=ffprobe)

    backup = sample_video.with_name(f"{sample_video.stem}_原画.mp4")
    assert backup.is_file()
    assert probe_fps(ffprobe, sample_video) == pytest.approx(24, abs=0.5)
    assert probe_fps(ffprobe, backup) == pytest.approx(30, abs=0.5)


def test_transcode_skips_when_already_target(sample_video, ffmpeg_pair):
    """源帧率就是目标帧率时不做无意义的重新编码。"""
    ffmpeg, ffprobe = ffmpeg_pair
    messages = []
    transcode(ffmpeg, sample_video, 30, ffprobe_path=ffprobe,
              notify=messages.append)

    assert any("无需转码" in m for m in messages)
