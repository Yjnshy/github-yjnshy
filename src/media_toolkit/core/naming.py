"""文件命名与链接提取。

统一输出命名规范：平台_作者_标题_时间戳
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import List

URL_RE = re.compile(r"https?://[^\s一-鿿，。；、！？\"'）】]+", re.I)
TRIM_CHARS = ".,;:!?)]}>'\"，。；：！？）】"

_ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|#\r\n\t]+')


def extract_urls(text: str) -> List[str]:
    """从任意粘贴文本（含分享文案）中提取链接，去重保序。"""
    found: List[str] = []
    seen = set()
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(TRIM_CHARS)
        if url and url not in seen:
            seen.add(url)
            found.append(url)
    return found


def safe_name(title: str, limit: int = 60) -> str:
    """清洗文件系统非法字符，截断长度。"""
    cleaned = _ILLEGAL_CHARS_RE.sub(" ", title or "").strip()
    return cleaned[:limit] or "未命名"


def build_filename(platform: str, author: str, title: str,
                   when: datetime | None = None, ext: str = "") -> str:
    """生成标准文件名：平台_作者_标题_时间戳[.ext]（不含路径）。"""
    when = when or datetime.now()
    parts = [safe_name(platform, 20)]
    if author and author.strip():
        parts.append(safe_name(author, 30))
    parts.append(safe_name(title, 60))
    stem = "_".join(parts)
    stamp = when.strftime("%Y%m%d_%H%M%S")
    name = f"{stem}_{stamp}"
    return name + ext if ext else name
