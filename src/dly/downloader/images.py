"""图文作品下载：无水印原图 + 文案存档，webp 自动转 jpg。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import httpx

from dly.core.naming import build_filename
from dly.platforms.base import UA_MOBILE, MediaInfo

logger = logging.getLogger(__name__)

_EXT_BY_CONTENT_TYPE = {
    "png": ".png", "webp": ".webp", "gif": ".gif",
    "heic": ".heic", "heif": ".heic",
}


def download_images(info: MediaInfo, outdir: Path, cookie: str,
                    progress: Optional[Callable[[str], None]] = None) -> int:
    """下载图文作品的全部原图，返回成功张数。"""
    notify = progress or (lambda msg: None)
    stem = build_filename(info.platform, info.author, info.title)
    multi = len(info.images) > 1
    dest = outdir / stem if multi else outdir
    dest.mkdir(parents=True, exist_ok=True)

    _save_text(dest / f"{stem}.txt", info)
    notify(f"图文 {len(info.images)} 张 → {dest}")

    saved = 0
    for index, url in enumerate(info.images, 1):
        name = f"{stem}_{index:02d}" if multi else stem
        try:
            path = _fetch_image(url, dest, name, cookie)
            path = webp_to_jpg(path)
            saved += 1
            notify(f"  ✓ {index}/{len(info.images)}  {path.name}")
        except Exception as exc:
            logger.error("第 %d 张下载失败: %s", index, exc)
    return saved


def _save_text(path: Path, info: MediaInfo) -> None:
    """把图文文案存成 txt 存档。"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write((info.text or "").strip() + "\n\n")
            if info.author:
                f.write(f"作者：{info.author}\n")
            if info.source_url:
                f.write(f"链接：{info.source_url}\n")
    except OSError as exc:
        logger.warning("文案保存失败: %s", exc)


def _fetch_image(url: str, dest: Path, stem: str, cookie: str) -> Path:
    headers = {
        "User-Agent": UA_MOBILE,
        "Referer": "https://www.douyin.com/",
    }
    if cookie:
        headers["Cookie"] = cookie
    with httpx.stream("GET", url, headers=headers, timeout=30,
                      follow_redirects=True) as resp:
        resp.raise_for_status()
        content_type = (resp.headers.get("Content-Type") or "").lower()
        ext = next((e for key, e in _EXT_BY_CONTENT_TYPE.items()
                    if key in content_type), ".jpg")
        path = dest / f"{stem}{ext}"
        with open(path, "wb") as f:
            for chunk in resp.iter_bytes(64 * 1024):
                f.write(chunk)
    if path.stat().st_size < 512:
        path.unlink(missing_ok=True)
        raise IOError("返回内容过小，可能触发了防盗链")
    return path


def webp_to_jpg(path: Path) -> Path:
    """webp 转 jpg 便于直接查看；失败则保留原文件。"""
    if path.suffix.lower() != ".webp":
        return path
    try:
        from PIL import Image
    except ImportError:
        return path
    try:
        with Image.open(path) as im:
            if im.mode in ("RGBA", "LA") or \
                    (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                converted = bg
            else:
                converted = im.convert("RGB")
            out = path.with_suffix(".jpg")
            converted.save(out, "JPEG", quality=95)
        if out.exists() and out.stat().st_size > 512:
            path.unlink()
            return out
    except Exception as exc:
        logger.debug("webp 转 jpg 失败: %s", exc)
    return path
