"""B 站直播间的补充信息（主播昵称 / UID / 标题）。

为什么单独来一嘴：B 站直播走的是 yt-dlp 的通用解析，元数据里
``uploader`` 经常是空的，导致录制目录只能叫「B站」，用户没法按主播找。
这里补一次官网房间接口（公开、无需登录），把主播名和房间号拿到手。

失败一律返回空，不影响录制——目录名退化成「B站」而已。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

INFO_API = "https://api.live.bilibili.com/room/v1/Room/get_info?room_id={}"
ANCHOR_API = ("https://api.live.bilibili.com/live_user/v1/UserInfo/"
              "get_anchor_in_room?roomid={}")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_ROOM_RE = re.compile(r"live\.bilibili\.com/(?:blanc/)?(\d+)", re.I)


def room_id_from_url(url: str) -> str:
    """从直播间链接里取房间号（``?spm_id_from=`` 这类参数不影响）。"""
    m = _ROOM_RE.search(url or "")
    return m.group(1) if m else ""


def _get_json(url: str, cookie: str, proxy: str) -> Optional[dict]:
    request = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://live.bilibili.com/",
                      **({"Cookie": cookie} if cookie else {})})
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("请求 %s 失败: %s", url, exc)
        return None


def fetch_room_meta(room_id: str, cookie: str = "",
                    proxy: str = "") -> Tuple[str, str, str]:
    """返回 ``(主播昵称, 主播UID, 直播标题)``，取不到的项为空串。

    两个接口分工：``get_info`` 有标题但已不再返回主播信息（anchor_info
    为 null），主播名要从 ``get_anchor_in_room`` 拿，所以各问一次。
    """
    if not room_id:
        return "", "", ""

    uname, uid, title = "", "", ""
    payload = _get_json(ANCHOR_API.format(room_id), cookie, proxy) or {}
    info = ((payload.get("data") or {}).get("info") or {})
    uname = str(info.get("uname") or "")
    uid = str(info.get("uid") or "")

    payload = _get_json(INFO_API.format(room_id), cookie, proxy) or {}
    if payload.get("code") == 0:
        data = payload.get("data") or {}
        title = str(data.get("title") or "")
        uid = uid or str(data.get("uid") or "")

    return uname, uid, title
