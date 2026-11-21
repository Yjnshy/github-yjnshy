"""本地歌单：入库、去重、封面、进度，以及音频接口的 Range 支持。

Range 是这一块最关键的地方：浏览器播音频时先发 ``bytes=0-``，拖动进度条
再发区间请求。服务端如果不回 206，时间轴会跳回开头、时长显示成 Infinity。
"""

from __future__ import annotations

import json
import urllib.error
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


def test_video_endpoint_supports_range(tmp_path, monkeypatch):
    """隧道视频走同一套 Range：拖进度、切歌时不能跳回开头。"""
    mp3 = tmp_path / "同名字幕.mp3"
    mp3.write_bytes(bytes(range(256)) * 4)
    mp4 = tmp_path / "同名字幕.mp4"
    mp4.write_bytes(bytes(range(256)) * 12)      # 3072 字节，同名兄弟视频
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "DEFAULT_PORT", 8922)
    ctx = srv.create_app(desktop=False)
    ctx.start()
    try:
        req = urllib.request.Request(
            ctx.url + "api/library/add",
            data=json.dumps({"path": str(mp3)}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as res:
            track = json.loads(res.read())["track"]

        with urllib.request.urlopen(ctx.url + "api/library", timeout=5) as res:
            assert json.loads(res.read())["tracks"][0]["has_video"] is True

        full = urllib.request.urlopen(
            f"{ctx.url}api/media/video?id={track['id']}", timeout=5)
        assert full.status == 200
        assert full.headers.get("Accept-Ranges") == "bytes"
        assert int(full.headers["Content-Length"]) == 3072

        req = urllib.request.Request(
            f"{ctx.url}api/media/video?id={track['id']}",
            headers={"Range": "bytes=100-199"})
        with urllib.request.urlopen(req, timeout=5) as part:
            assert part.status == 206
            assert part.headers["Content-Range"] == "bytes 100-199/3072"
            assert len(part.read()) == 100

        req = urllib.request.Request(
            f"{ctx.url}api/media/video?id={track['id']}",
            headers={"Range": "bytes=-16"})
        with urllib.request.urlopen(req, timeout=5) as tail:
            assert tail.status == 206
            assert tail.headers["Content-Range"] == "bytes 3056-3071/3072"
    finally:
        ctx.shutdown()


def test_tunnel_assets_are_served(tmp_path, monkeypatch):
    """三维视图用到三个脚本：隧道模块本身 + 内置的 three.js 两个包。"""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "DEFAULT_PORT", PORT + 1)
    ctx = srv.create_app(desktop=False)
    ctx.start()
    try:
        for name in ("music-tunnel.js", "three/three.module.min.js",
                     "three/three.core.min.js",
                     "three/addons/postprocessing/EffectComposer.js",
                     "three/addons/postprocessing/UnrealBloomPass.js",
                     "three/addons/shaders/CopyShader.js"):
            with urllib.request.urlopen(ctx.url + name, timeout=8) as res:
                assert res.status == 200, name
                assert "javascript" in res.headers.get("Content-Type", "")
                assert len(res.read()) > 200, name
        # 白名单之外的文件不能被读到
        try:
            urllib.request.urlopen(ctx.url + "config.example.yaml", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("静态白名单失效，任意文件被暴露")
    finally:
        ctx.shutdown()


def test_frames_need_original_video(tmp_path, monkeypatch, fake_mp3: Path):
    """纯音频入库的曲目没有原视频，换封面接口要如实返回空列表而不是报错。"""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "DEFAULT_PORT", PORT + 2)
    ctx = srv.create_app(desktop=False)
    ctx.start()
    try:
        body = json.dumps({"path": str(fake_mp3)}).encode()
        req = urllib.request.Request(
            ctx.url + "api/library/add", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as res:
            track = json.loads(res.read())["track"]

        req = urllib.request.Request(
            ctx.url + "api/library/frames",
            data=json.dumps({"id": track["id"]}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as res:
            assert json.loads(res.read())["frames"] == []

        req = urllib.request.Request(
            ctx.url + "api/library/cover",
            data=json.dumps({"id": track["id"], "i": 99}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError("不存在的帧不该被接受")
    finally:
        ctx.shutdown()


def test_cover_uses_second_two():
    """封面默认取视频第二秒（开头常是黑场/台标，第 2 秒更稳定）。"""
    import dly.core.library as libmod
    src = __import__("inspect").getsource(libmod.make_cover)
    assert '"-ss", "2"' in src or '"-ss",\'2\'' in src.replace(" ", "")
    assert "第 2 秒" in src or "第二秒" in src


def test_extract_supports_batch_paths(tmp_path, monkeypatch, fake_mp3: Path):
    """「从视频提取」批量：paths 数组一次提交，逐个返回成功/失败。"""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(srv, "DEFAULT_PORT", PORT + 3)
    ctx = srv.create_app(desktop=False)
    ctx.start()
    try:
        body = json.dumps({"paths": [str(fake_mp3)]}).encode()
        req = urllib.request.Request(
            ctx.url + "api/library/extract", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.loads(res.read())
        assert data["ok"] is True
        assert len(data["tracks"]) == 1
        assert data["failed"] == []
        assert "tracks_all" in data
    finally:
        ctx.shutdown()

