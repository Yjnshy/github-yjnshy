"""本地歌单：入库、去重、封面、进度，以及音频接口的 Range 支持。

Range 是这一块最关键的地方：浏览器播音频时先发 ``bytes=0-``，拖动进度条
再发区间请求。服务端如果不回 206，时间轴会跳回开头、时长显示成 Infinity。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from dly.core import paths
from dly.core.library import Library
from dly.gui import server as srv

PORT = 8921


@pytest.fixture
def fake_mp3(tmp_path: Path) -> Path:
    """随便一段字节当 mp3 用：接口只负责按字节发，不解析内容。"""
    path = tmp_path / "测试音频.mp3"
    path.write_bytes(bytes(range(256)) * 8)      # 2048 字节，便于算区间
    return path


@pytest.fixture
def lib(tmp_path, monkeypatch) -> Library:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return Library()


def test_add_dedupes_and_persists(lib: Library, fake_mp3: Path):
    first = lib.add(fake_mp3, source="download")
    assert first["id"] == 1
    assert first["title"] == "测试音频"
    assert Path(first["path"]) == fake_mp3.resolve()

    again = lib.add(fake_mp3)
    assert again["id"] == first["id"], "同一个文件重复加入不该产生第二条"
    assert len(lib.public()) == 1

    # 换一个实例读回来，证明落盘了
    monkeypatch_root = lib.root
    reloaded = Library(monkeypatch_root)
    assert [t["id"] for t in reloaded.public()] == [1]


def test_last_pos_roundtrip(lib: Library, fake_mp3: Path):
    track = lib.add(fake_mp3)
    lib.update(track["id"], last_pos=61.5)
    assert lib.public()[0]["last_pos"] == 61.5
    assert Library(lib.root).public()[0]["last_pos"] == 61.5


def test_cover_generated_and_missing_flag(lib: Library, fake_mp3: Path):
    track = lib.add(fake_mp3)
    assert track["cover"] and Path(track["cover"]).is_file()
    assert Path(track["cover"]).stat().st_size > 0
    assert lib.public()[0]["has_cover"] is True

    fake_mp3.unlink()
    assert lib.public()[0]["missing"] is True


def test_remove_keeps_file_by_default(lib: Library, fake_mp3: Path):
    track = lib.add(fake_mp3)
    assert lib.remove(track["id"]) is True
    assert lib.public() == []
    assert fake_mp3.exists(), "默认只从歌单移除，不删用户的文件"

    track = lib.add(fake_mp3)
    assert lib.remove(track["id"], delete_file=True) is True
    assert not fake_mp3.exists()


def test_audio_endpoint_supports_range(tmp_path, monkeypatch, fake_mp3: Path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "DEFAULT_PORT", PORT)
    ctx = srv.create_app(desktop=False)
    ctx.start()
    try:
        body = json.dumps({"path": str(fake_mp3)}).encode()
        req = urllib.request.Request(
            ctx.url + "api/library/add", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as res:
            assert res.status == 200
            track = json.loads(res.read())["track"]

        with urllib.request.urlopen(ctx.url + "api/library", timeout=5) as res:
            listing = json.loads(res.read())
        assert len(listing["tracks"]) == 1
        assert listing["tracks"][0]["title"] == "测试音频"

        full = urllib.request.urlopen(
            f"{ctx.url}api/media/audio?id={track['id']}", timeout=5)
        assert full.status == 200
        assert full.headers.get("Accept-Ranges") == "bytes"
        assert int(full.headers["Content-Length"]) == 2048

        req = urllib.request.Request(
            f"{ctx.url}api/media/audio?id={track['id']}",
            headers={"Range": "bytes=100-199"})
        with urllib.request.urlopen(req, timeout=5) as part:
            assert part.status == 206
            assert part.headers["Content-Range"] == "bytes 100-199/2048"
            assert len(part.read()) == 100

        req = urllib.request.Request(
            f"{ctx.url}api/media/audio?id={track['id']}",
            headers={"Range": "bytes=-16"})
        with urllib.request.urlopen(req, timeout=5) as tail:
            assert tail.status == 206
            assert tail.headers["Content-Range"] == "bytes 2032-2047/2048"

        cover = urllib.request.urlopen(
            f"{ctx.url}api/media/cover?id={track['id']}", timeout=5)
        assert cover.status == 200
        assert cover.headers.get("Content-Type") == "image/jpeg"
        assert len(cover.read()) > 0
    finally:
        ctx.shutdown()
