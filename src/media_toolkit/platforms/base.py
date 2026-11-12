"""平台适配器抽象基类与数据模型。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from media_toolkit.cookies.manager import CookieManager

# 通用 UA
UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")


@dataclass
class MediaInfo:
    """一条已解析的下载目标。"""
    type: str                       # "video" | "images"
    title: str
    platform: str
    author: str = ""
    url: str = ""                   # type=video 时的无水印直链（为空则交给 yt-dlp）
    images: List[str] = field(default_factory=list)  # type=images 时的原图列表
    text: str = ""                  # 图文文案
    label: str = ""                 # 可读的画质/数量描述
    referer: str = ""               # 下载直链时需要带的 Referer
    source_url: str = ""            # 原始分享链接（存档用）


@dataclass
class LiveInfo:
    """一条直播间的实时状态。"""
    platform: str
    room_url: str
    is_live: bool
    title: str = ""
    anchor: str = ""
    stream_url: str = ""            # 优先 flv，其次 hls
    referer: str = ""


class PlatformAdapter(ABC):
    """所有平台适配器的统一接口。"""

    name: str = "generic"

    def __init__(self, cookies: CookieManager, proxy: str = ""):
        self.cookies = cookies
        self.proxy = proxy

    @abstractmethod
    def match(self, url: str) -> bool:
        """判断本适配器是否处理该 URL。"""

    def resolve(self, url: str, quality: str = "best") -> Optional[MediaInfo]:
        """解析下载目标。返回 None 表示交给 yt-dlp 兜底。"""
        return None

    def get_live_info(self, room_url: str) -> Optional[LiveInfo]:
        """查询直播间状态与流地址。返回 None 表示该平台不支持直播录制。"""
        return None
