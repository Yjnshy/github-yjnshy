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


PLATFORM_LABELS = {
    "douyin": "抖音",
    "bilibili": "B站",
    "xiaohongshu": "小红书",
}


def platform_label(platform: str) -> str:
    """平台的中文名（用于目录名）；未知平台原样保留。"""
    key = (platform or "").lower()
    return PLATFORM_LABELS.get(key) or safe_name(platform or "其他", 20)


def live_folder_name(platform: str, anchor: str, anchor_id: str = "") -> str:
    """直播录制目录名：``平台-主播昵称-主播号``。

    主播号优先用平台自己的 ID（抖音号 / 房间号），拿不到就省略这一段，
    避免出现 ``抖音-Katie小仙女-`` 这种尾巴。
    """
    parts = [platform_label(platform)]
    if anchor and anchor.strip():
        parts.append(safe_name(anchor, 30))
    if anchor_id:
        parts.append(safe_name(str(anchor_id), 24))
    return "-".join(parts)


def live_filename(title: str, when: datetime | None = None, ext: str = "") -> str:
    """直播文件名：``标题_录制时间[.ext]``，方便按时间排序查找。"""
    when = when or datetime.now()
    stem = f"{safe_name(title or '直播', 60)}_{when.strftime('%Y%m%d_%H%M%S')}"
    return f"{stem}.{ext}" if ext else stem
