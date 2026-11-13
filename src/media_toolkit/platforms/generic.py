"""通用适配器：基于 yt-dlp 的兜底实现。

覆盖 B站、小红书、YouTube 等 yt-dlp 已支持的 1000+ 站点，
包括 B站直播间（yt-dlp 原生支持 live.bilibili.com）。
"""

from __future__ import annotations

import logging
from typing import Optional

from yt_dlp import YoutubeDL

from media_toolkit.cookies.manager import PLATFORM_DOMAINS
from media_toolkit.platforms.base import (
    UA_PC, LiveInfo, MediaInfo, PlatformAdapter,
)

logger = logging.getLogger(__name__)

_HOST_TO_PLATFORM = {
    "bilibili.com": "bilibili",
    "b23.tv": "bilibili",
    "xiaohongshu.com": "xiaohongshu",
    "xhslink.com": "xiaohongshu",
}


class GenericAdapter(PlatformAdapter):
    name = "generic"

    def match(self, url: str) -> bool:
        return url.startswith("http")  # 永远兜底

    def resolve(self, url: str, quality: str = "best") -> Optional[MediaInfo]:
        """只探测元数据用于命名，实际下载交给引擎里的 yt-dlp。"""
        info = self._extract(url)
        if not info:
            return None
        platform = self._platform_of(url)
        return MediaInfo(
            type="video",
            title=info.get("title") or "未命名",
            author=info.get("uploader") or info.get("channel") or "",
            platform=platform,
            url="",  # 空 url = 由 yt-dlp 直接下载原始链接
            label=info.get("resolution") or "",
            source_url=url,
        )

    def get_live_info(self, room_url: str) -> Optional[LiveInfo]:
        info = self._extract(room_url)
        if not info:
            return None
        is_live = bool(info.get("is_live"))
        platform = self._platform_of(room_url)
        live = LiveInfo(
            platform=platform,
            room_url=room_url,
            is_live=is_live,
            title=info.get("title") or "",
            # 直播元数据里上传者字段因平台而异，逐个兜底
            anchor=(info.get("uploader") or info.get("channel")
                    or info.get("uploader_id") or ""),
            referer=info.get("webpage_url") or room_url,
            user_agent=UA_PC,
            cookie=self.cookies.get_header(platform) if platform != "other" else "",
        )
        if is_live:
            # 优先 flv（延迟低、不易断），其次 hls
            formats = info.get("formats") or []
            flv = next((f["url"] for f in formats
                        if isinstance(f.get("url"), str) and ".flv" in f["url"]), "")
            hls = next((f["url"] for f in formats
                        if isinstance(f.get("url"), str) and ".m3u8" in f["url"]), "")
            live.stream_url = flv or hls or info.get("url") or ""
        return live

    # ------------------------------------------------------------------

    def _extract(self, url: str) -> Optional[dict]:
        platform = self._platform_of(url)
        cookie_file = self.cookies.write_netscape_file(platform) \
            if platform in PLATFORM_DOMAINS else None
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }
        if cookie_file:
            opts["cookiefile"] = cookie_file
        if self.proxy:
            opts["proxy"] = self.proxy
        try:
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:
            logger.debug("yt-dlp 元数据探测失败: %s", exc)
            return None

    @staticmethod
    def _platform_of(url: str) -> str:
        for host, name in _HOST_TO_PLATFORM.items():
            if host in url:
                return name
        return "other"
