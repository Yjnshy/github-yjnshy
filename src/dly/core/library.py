"""本地歌单：把下载或提取出来的音频收进一个可播放的列表。

所有东西都放在用户数据目录 ``data_dir()/music``：

- ``library.json``   曲目列表（绝对路径，重启不丢）
- ``covers/<id>.jpg`` 512×512 封面

封面优先用 ffmpeg 从视频抽一帧（音频则尝试读内嵌封面），
没有 ffmpeg 时用 PIL 画一张渐变占位，保证列表永远有图可显示。

文件被用户手动删掉时条目仍然保留，只标记 ``missing``，
前端把它灰掉、不参与播放——比默默消失更好解释。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dly.core import compat, paths
from dly.core.ffmpeg import find_ffmpeg, probe

logger = logging.getLogger(__name__)

MAX_TRACKS = 500
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
VIDEO_EXT = {".mp4", ".mkv", ".flv", ".mov", ".webm", ".ts", ".avi", ".m4v", ".mpg"}


def is_audio(path: str | Path) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXT


def is_video(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXT


def _run(cmd: List[str], timeout: float = 180) -> bool:
    try:
        done = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout,
                              **compat.popen_kwargs())
        return done.returncode == 0
    except Exception as exc:  # noqa: BLE001 抽不出来不算致命
        logger.debug("命令执行失败 %s: %s", cmd[0], exc)
        return False


def _ffmpeg_path() -> str:
    from dly.core.config import load_config   # 避免循环导入
    try:
        return find_ffmpeg(load_config().ffmpeg_path) or ""
    except Exception:  # noqa: BLE001
        return ""


def duration_of(path: str | Path) -> float:
    """时长（秒）；读不到返回 0，前端按 0 处理成「未知」。"""
    try:
        info = probe(str(path))
    except Exception:  # noqa: BLE001
        return 0.0
    return float(info[2]) if info else 0.0


def _gradient_cover(out: Path, seed: str) -> bool:
    """没有 ffmpeg / 抽不到帧时的兜底封面：按标题哈希出一张柔和渐变。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        return False
    try:
        digest = hashlib.sha256((seed or "dly").encode("utf-8")).digest()
        top = (40 + digest[0] % 90, 40 + digest[1] % 80, 90 + digest[2] % 90)
        bottom = (12 + digest[3] % 40, 12 + digest[4] % 40, 28 + digest[5] % 50)
        img = Image.new("RGB", (512, 512))
        draw = ImageDraw.Draw(img)
        for y in range(512):
            t = y / 511
            draw.line([(0, y), (512, y)],
                      fill=tuple(int(top[i] + (bottom[i] - top[i]) * t)
                                 for i in range(3)))
        # 左上角一层很淡的高光，避免纯渐变显得死板（重复画椭圆堆出柔和边缘）
        overlay = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        for r in range(240, 0, -10):
            odraw.ellipse([256 - r, 150 - r, 256 + r, 150 + r],
                          fill=(255, 250, 255, 4))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        img.save(out, "JPEG", quality=88)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("生成占位封面失败: %s", exc)
        return False


def make_cover(src: str | Path, out: Path, title: str = "") -> bool:
    """给曲目生成 512×512 封面：视频抽帧 > 内嵌封面 > 渐变。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _ffmpeg_path()
    src = str(src)
    if ffmpeg:
        scale = ("scale=512:512:force_original_aspect_ratio=increase,"
                 "crop=512:512")
        # 视频：取第 1 秒（开头常是黑场或台标）
        if is_video(src) and _run([ffmpeg, "-y", "-ss", "1", "-i", src,
                                   "-frames:v", "1", "-vf", scale,
                                   "-q:v", "3", str(out)], 90):
            return True
        # 音频：读 ID3 里内嵌的那张
        if is_audio(src) and _run([ffmpeg, "-y", "-i", src, "-map", "0:v:0",
                                   "-frames:v", "1", "-vf", scale,
                                   "-q:v", "3", str(out)], 90):
            return True
        # 视频不足 1 秒（或首帧解码失败）时退回第 0 帧
        if _run([ffmpeg, "-y", "-i", src, "-frames:v", "1", "-vf", scale,
                 "-q:v", "3", str(out)], 90):
            return True
    return _gradient_cover(out, title or Path(src).stem)


def extract_audio(src: str | Path, out: Path | None = None) -> Optional[Path]:
    """从视频里抽一条 mp3；成功返回目标路径。"""
    ffmpeg = _ffmpeg_path()
    src_path = Path(src)
    if not ffmpeg or not src_path.is_file():
        return None
    target = out or src_path.with_suffix(".mp3")
    if target.exists() and target.stat().st_size > 0:
        return target
    ok = _run([ffmpeg, "-y", "-i", str(src_path), "-vn",
               "-acodec", "libmp3lame", "-q:a", "2", str(target)], 900)
    if ok and target.is_file() and target.stat().st_size > 0:
        return target
    target.unlink(missing_ok=True)
    return None


class Library:
    """曲目表的读写。所有改动都落盘，进程重启后还在。"""

    def __init__(self, root: Path | None = None) -> None:
        self._lock = threading.Lock()
        self.root = Path(root) if root else paths.data_dir() / "music"
        self.covers = self.root / "covers"
        self.file = self.root / "library.json"
        self.tracks: List[Dict[str, Any]] = self._load()
        self._seq = max((int(t.get("id") or 0) for t in self.tracks), default=0)

    # ---- 读写 ----

    def _load(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        rows: List[Dict[str, Any]] = []
        for item in data[-MAX_TRACKS:]:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            rows.append({
                "id": int(item.get("id") or 0),
                "title": str(item.get("title") or ""),
                "path": str(item.get("path")),
                "cover": str(item.get("cover") or ""),
                "duration": float(item.get("duration") or 0),
                "size": int(item.get("size") or 0),
                "added_at": float(item.get("added_at") or 0),
                "last_pos": float(item.get("last_pos") or 0),
                "source": str(item.get("source") or "import"),
            })
        return rows

    def _save(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.tracks, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError as exc:
            logger.debug("保存歌单失败: %s", exc)

    # ---- 查询 ----

    def public(self) -> List[Dict[str, Any]]:
        """给前端的视图：不暴露封面真实路径，只给 id 让 /api/media/cover 去取。"""
        with self._lock:
            rows = []
            for t in self.tracks:
                missing = not Path(t["path"]).is_file()
                rows.append({
                    "id": t["id"],
                    "title": t["title"] or Path(t["path"]).stem,
                    "duration": round(t["duration"], 1),
                    "last_pos": round(t["last_pos"], 1),
                    "size": t["size"],
                    "added_at": t["added_at"],
                    "source": t["source"],
                    "missing": missing,
                    "has_cover": bool(t["cover"]) and Path(t["cover"]).is_file(),
                })
            return rows

    def get(self, track_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            for t in self.tracks:
                if t["id"] == track_id:
                    return dict(t)
        return None

    def cover_of(self, track_id: int) -> Optional[Path]:
        track = self.get(track_id)
        if not track or not track["cover"]:
            return None
        path = Path(track["cover"])
        return path if path.is_file() else None

    # ---- 写入 ----

    def add(self, path: str | Path, title: str = "",
            cover_src: str | Path | None = None,
            source: str = "import") -> Dict[str, Any]:
        """收进歌单。同一个文件重复添加时只更新标题/封面，不产生重复条目。"""
        target = Path(path).expanduser().resolve()
        if not target.is_file():
            raise FileNotFoundError(f"文件不存在：{target}")
        with self._lock:
            for t in self.tracks:
                if Path(t["path"]) == target:
                    if title:
                        t["title"] = title
                    self._save()
                    return dict(t)
            self._seq += 1
            track_id = self._seq
            cover = self.covers / f"{track_id}.jpg"
            if not make_cover(cover_src or target, cover, title or target.stem):
                cover = Path("")
            row = {
                "id": track_id,
                "title": title or target.stem,
                "path": str(target),
                "cover": str(cover) if str(cover) else "",
                "duration": duration_of(target),
                "size": target.stat().st_size,
                "added_at": time.time(),
                "last_pos": 0.0,
                "source": source,
            }
            self.tracks.append(row)
            while len(self.tracks) > MAX_TRACKS:
                old = self.tracks.pop(0)
                try:
                    Path(old["cover"]).unlink(missing_ok=True)
                except OSError:
                    pass
            self._save()
            return dict(row)

    def update(self, track_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = {"title", "last_pos"}
        with self._lock:
            for t in self.tracks:
                if t["id"] != track_id:
                    continue
                for key, value in fields.items():
                    if key not in allowed:
                        continue
                    if key == "last_pos":
                        t[key] = max(0.0, float(value or 0))
                    else:
                        t[key] = str(value)
                self._save()
                return dict(t)
        return None

    def remove(self, track_id: int, delete_file: bool = False) -> bool:
        with self._lock:
            for index, t in enumerate(self.tracks):
                if t["id"] != track_id:
                    continue
                self.tracks.pop(index)
                try:
                    Path(t["cover"]).unlink(missing_ok=True)
                except OSError:
                    pass
                if delete_file:
                    try:
                        Path(t["path"]).unlink(missing_ok=True)
                    except OSError:
                        logger.debug("删除音频文件失败: %s", t["path"])
                self._save()
                return True
        return False

    def add_extracted(self, video: str | Path, title: str = "") -> Dict[str, Any]:
        """从视频抽一条 mp3（放在原视频旁边）再入库，封面取视频首帧。"""
        video_path = Path(video)
        audio = extract_audio(video_path)
        if not audio:
            raise RuntimeError("提取音频失败：请确认 ffmpeg 可用、文件没有损坏")
        return self.add(audio, title=title or video_path.stem,
                        cover_src=video_path, source="extract")
