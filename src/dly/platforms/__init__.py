"""平台适配层：统一接口，按 URL 路由到对应平台。"""

from __future__ import annotations

from typing import Optional

from dly.cookies.manager import CookieManager
from dly.platforms.base import LiveInfo, MediaInfo, PlatformAdapter
from dly.platforms.douyin import DouyinAdapter
from dly.platforms.generic import GenericAdapter

__all__ = [
    "LiveInfo", "MediaInfo", "PlatformAdapter",
    "DouyinAdapter", "GenericAdapter", "find_adapter",
]


def find_adapter(url: str, cookies: CookieManager,
                 proxy: str = "") -> Optional[PlatformAdapter]:
    """按 URL 选择合适的平台适配器，GenericAdapter 永远兜底。"""
    for cls in (DouyinAdapter, GenericAdapter):
        adapter = cls(cookies=cookies, proxy=proxy)
        if adapter.match(url):
            return adapter
    return None
