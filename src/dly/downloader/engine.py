"""下载引擎：解析 → 命名 → yt-dlp / 直链下载。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Optional

from yt_dlp import YoutubeDL

from dly.core.config import AppConfig
from dly.core.ffmpeg import find_ffmpeg, find_ffprobe
from dly.core.naming import build_filename, extract_urls
from dly.core.transcode import (TranscodeError, is_transcodable,
                                          normalize_fps, transcode)
from dly.cookies.manager import CookieManager
from dly.downloader.images import download_images
from dly.platforms import find_adapter
from dly.platforms.base import MediaInfo, UA_MOBILE

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
        self.ffprobe = find_ffprobe(self.ffmpeg, config.ffprobe_path)
        self._notify = progress or (lambda msg: None)

    def download_text(self, text: str, quality: Optional[str] = None,
                      output_dir: Optional[str] = None,
                      fps: Optional[int] = None) -> tuple[int, int]:
        """从任意文本（含分享文案）中提取链接并批量下载。

        返回 (成功数, 总数)。
        """
        urls = extract_urls(text)
        if not urls:
            self._notify("没有从输入中识别到链接")
            return 0, 0
        quality = quality or self.config.quality
        fps = normalize_fps(self.config.fps if fps is None else fps)
        outdir = Path(output_dir or self.config.output_dir)
        outdir.mkdir(parents=True, exist_ok=True)

        success = 0
        for index, url in enumerate(urls, 1):
            self._notify(f"[{index}/{len(urls)}] {url}")
            try:
                if self.download_one(url, quality, outdir, fps=fps):
                    success += 1
            except Exception as exc:
                logger.error("下载失败 %s: %s", url, exc)
        self.cookies.cleanup()
        return success, len(urls)

    def download_one(self, url: str, quality: str, outdir: Path,
                     fps: int = 0) -> bool:
        adapter = find_adapter(url, self.cookies, proxy=self.config.proxy)
        if adapter is None:
            logger.error("无法识别的链接: %s", url)
            return False

        info: Optional[MediaInfo] = adapter.resolve(url, quality)
        if info and info.type == "images":
            return self._download_image_post(info, outdir)

        # 无水印直链（抖音）或 yt-dlp 兜底
        target_url = info.url if info and info.url else url
        platform = (info.platform if info else None) or adapter.name
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
        if getattr(self.config, "compat_format", True) and self.ffmpeg:
            # 同分辨率优先 H.264：B 站等会给 AV1/HEVC，Windows 自带播放器放不了，
            # 用户看到「格式错误」会以为下载坏了。只是排序，没有 H.264 时仍会用别的。
            opts["format_sort"] = ["vcodec:h264"]
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
        self._apply_fps(outdir, stem, fps)
        return True

    # ------------------------------------------------------------------
    # 帧率转换（下载完成后进行，源帧率固定只能重编码）
    # ------------------------------------------------------------------

    def _apply_fps(self, outdir: Path, stem: str, fps: int) -> None:
        fps = normalize_fps(fps)
        if not fps:
            return
        target = self._locate_output(outdir, stem)
        if target is None:
            self._notify("未找到下载产物，已跳过帧率转换")
            return
        if not is_transcodable(target):
            self._notify(f"{target.suffix} 不支持帧率转换，已跳过")
            return
        if not self.ffmpeg:
            self._notify("缺少 ffmpeg，无法转换帧率")
            return
        try:
            transcode(self.ffmpeg, target, fps,
                      keep_original=self.config.keep_original,
                      ffprobe_path=self.ffprobe,
                      notify=self._notify)
        except TranscodeError as exc:
            self._notify(f"✗ 帧率转换失败：{exc}")
            for line in (exc.detail or "").splitlines()[-3:]:
                self._notify(f"  {line}")

    @staticmethod
    def _locate_output(outdir: Path, stem: str) -> Optional[Path]:
        """按命名模板找到实际落盘文件（yt-dlp 会追加扩展名）。"""
        # 元数据探测失败时 stem 里含 %(uploader)s 之类的模板字段，退化为前缀匹配
        pattern = stem if "%(" not in stem else stem.split("%(")[0] + "*"
        candidates = [
            p for p in outdir.glob(pattern + ".*")
            if p.is_file() and "__fps_tmp" not in p.name
            and p.suffix.lower() not in (".part", ".ytdl", ".temp")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

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
