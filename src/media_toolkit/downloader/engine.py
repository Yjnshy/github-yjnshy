"""下载引擎：解析 → 命名 → yt-dlp / 直链下载。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, List, Optional

from yt_dlp import YoutubeDL

from media_toolkit.core.config import AppConfig
from media_toolkit.core.ffmpeg import find_ffmpeg
from media_toolkit.core.naming import build_filename, extract_urls
from media_toolkit.cookies.manager import CookieManager
from media_toolkit.downloader.images import download_images
from media_toolkit.platforms import find_adapter
from media_toolkit.platforms.base import MediaInfo, UA_MOBILE

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

_FORMAT_MAP = {
    "best": "bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720": "bv*[height<=720]+ba/b[height<=720]",
    "480": "bv*[height<=480]+ba/b[height<=480]",
    "audio": "bestaudio/best",
    "mp3": "bestaudio/best",
}


class DownloadEngine:
    """批量下载编排：链接解析、平台分发、命名落盘。"""

    def __init__(self, config: AppConfig,
                 progress: Optional[ProgressCallback] = None):
        self.config = config
        self.cookies = CookieManager(config.cookies)
        self.ffmpeg = find_ffmpeg(config.ffmpeg_path)
        self._notify = progress or (lambda msg: None)

    def download_text(self, text: str, quality: Optional[str] = None,
                      output_dir: Optional[str] = None) -> tuple[int, int]:
        """从任意文本（含分享文案）中提取链接并批量下载。

        返回 (成功数, 总数)。
        """
        urls = extract_urls(text)
        if not urls:
            self._notify("没有从输入中识别到链接")
            return 0, 0
        quality = quality or self.config.quality
        outdir = Path(output_dir or self.config.output_dir)
        outdir.mkdir(parents=True, exist_ok=True)

        success = 0
        for index, url in enumerate(urls, 1):
            self._notify(f"[{index}/{len(urls)}] {url}")
            try:
                if self.download_one(url, quality, outdir):
                    success += 1
            except Exception as exc:
                logger.error("下载失败 %s: %s", url, exc)
        self.cookies.cleanup()
        return success, len(urls)

    def download_one(self, url: str, quality: str, outdir: Path) -> bool:
        adapter = find_adapter(url, self.cookies, proxy=self.config.proxy)
        if adapter is None:
            logger.error("无法识别的链接: %s", url)
            return False

        info: Optional[MediaInfo] = adapter.resolve(url, quality)
        if info and info.type == "images":
            return self._download_image_post(info, outdir)

        # 无水印直链（抖音）或 yt-dlp 兜底
        target_url = info.url if info and info.url else url
        platform = info.platform if info else "other"
        referer = info.referer if info else ""

        if info:
            # 已拿到元数据：按「平台_作者_标题_时间戳」命名
            stem = build_filename(platform, info.author, info.title)
        else:
            # 元数据探测失败：让 yt-dlp 用自己的字段命名
            stem = f"{platform}_%(uploader)s_%(title).60s_%(epoch)s"
        outtmpl = str(outdir / (stem + ".%(ext)s"))

        opts = {
            "outtmpl": outtmpl,
            "format": _FORMAT_MAP.get(quality, "bv*+ba/b"),
            "noplaylist": True,
            "retries": 5,
            "fragment_retries": 5,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [self._hook],
        }
        if self.ffmpeg:
            opts["ffmpeg_location"] = os.path.dirname(self.ffmpeg)
        else:
            opts["format"] = "b"  # 无 ffmpeg 时退化为单文件流
        if quality in ("audio", "mp3") and self.ffmpeg:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            }]
        if referer:
            opts["http_headers"] = {
                "Referer": referer,
                "User-Agent": UA_MOBILE,
            }
        if self.config.proxy:
            opts["proxy"] = self.config.proxy
        cookie_file = self.cookies.write_netscape_file(platform)
        if cookie_file and not referer:
            # 直链下载不带 Cookie；yt-dlp 原始链接才需要
            opts["cookiefile"] = cookie_file

        with YoutubeDL(opts) as ydl:
            ydl.download([target_url])
        self._notify(f"✓ 完成：{Path(outtmpl).stem}")
        return True

    def _download_image_post(self, info: MediaInfo, outdir: Path) -> bool:
        cookie = self.cookies.get_header(info.platform)
        saved = download_images(info, outdir, cookie, progress=self._notify)
        return saved > 0

    def _hook(self, data: dict) -> None:
        if data.get("status") == "downloading":
            pct = (data.get("downloaded_bytes") or 0) / \
                max(data.get("total_bytes") or data.get("total_bytes_estimate") or 1, 1)
            self._notify(f"  下载中 {pct * 100:.1f}%  "
                         f"{data.get('_speed_str', '').strip()}")
        elif data.get("status") == "finished":
            self._notify("  下载完成，正在处理…")
