"""抖音平台适配器。

视频/图文下载：iesdouyin.com 分享页的 _ROUTER_DATA → playwm 替换 play（无水印）
直播录制：live.douyin.com 房间页的 RENDER_DATA → flv / hls 流地址

直播解析思路参考开源项目 DouyinLiveRecorder（MIT License）。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx

from media_toolkit.platforms.base import (
    UA_MOBILE, UA_PC, LiveInfo, MediaInfo, PlatformAdapter,
)

logger = logging.getLogger(__name__)

_QUALITY_TO_RATIO = {
    "best": "1080p", "1080": "1080p", "720": "720p",
    "480": "540p", "audio": "1080p", "mp3": "1080p",
}
_RATIO_LABEL = {"1080p": "1080P", "720p": "720P", "540p": "540P"}


class DouyinAdapter(PlatformAdapter):
    name = "douyin"

    def match(self, url: str) -> bool:
        return "douyin.com" in url or "iesdouyin.com" in url

    # ------------------------------------------------------------------
    # 视频 / 图文解析
    # ------------------------------------------------------------------

    def resolve(self, url: str, quality: str = "best") -> Optional[MediaInfo]:
        # 直播链接不走下载解析
        if "live.douyin.com" in url:
            return None

        cookie = self.cookies.get_header(self.name)
        if not cookie:
            logger.error("抖音解析需要 Cookie（浏览器自动提取失败时，"
                         "请确认已在浏览器登录 douyin.com）")
            return None

        ratio = _QUALITY_TO_RATIO.get(quality, "1080p")
        label = _RATIO_LABEL.get(ratio, ratio)

        try:
            final_url = self._follow_redirect(url, cookie)
            m = re.search(r"/(?:video|note|slides)/(\d+)", final_url or "")
            if not m:
                logger.warning("未能从链接提取作品 ID，交给 yt-dlp 处理")
                return None
            item_id = m.group(1)

            router_data = self._fetch_router_data(item_id, cookie)
            if router_data is None:
                logger.error("抖音分享页未返回数据，Cookie 可能已失效或被风控")
                return None
            item = find_item(router_data)
            if not item:
                logger.error("作品数据为空：可能已被删除或设为私密")
                return None

            title = item.get("desc") or item.get("caption") or "抖音作品"
            author = (item.get("author") or {}).get("nickname") or ""
            base = dict(title=title, platform=self.name, author=author,
                        source_url=url, referer="https://www.douyin.com/")

            # 图文作品：无水印原图 + 文案
            images = pick_image_urls(item)
            if images and quality not in ("audio", "mp3"):
                return MediaInfo(type="images", images=images,
                                 text=item.get("desc") or "",
                                 label=f"图文 {len(images)} 张", **base)

            # 视频作品：playwm -> play 得无水印地址
            play_addr = (item.get("video") or {}).get("play_addr") or {}
            url_list = play_addr.get("url_list") or []
            if not url_list:
                if images:
                    return MediaInfo(type="images", images=images,
                                     label=f"图文 {len(images)} 张", **base)
                logger.error("该作品没有可下载的视频地址")
                return None
            play = url_list[0].replace("playwm", "play")
            play = re.sub(r"ratio=[^&]*", f"ratio={ratio}", play)
            return MediaInfo(type="video", url=play, label=label, **base)

        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                logger.error("抖音返回 HTTP %d：Cookie 已失效或被风控，"
                             "请在浏览器重新登录 douyin.com 后重试", code)
            else:
                logger.error("请求失败：HTTP %d", code)
            return None
        except httpx.HTTPError as exc:
            logger.error("抖音解析网络错误: %s", exc)
            return None

    def _client(self, cookie: str, mobile: bool = True) -> httpx.Client:
        return httpx.Client(
            headers={
                "User-Agent": UA_MOBILE if mobile else UA_PC,
                "Cookie": cookie,
                "Referer": "https://www.douyin.com/",
            },
            follow_redirects=True,
            timeout=25,
            proxy=self.proxy or None,
        )

    def _follow_redirect(self, url: str, cookie: str) -> str:
        with self._client(cookie) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return str(resp.url)

    def _fetch_router_data(self, item_id: str, cookie: str) -> Optional[Dict]:
        """视频页和图文页的分享接口不同，逐个尝试。"""
        with self._client(cookie) as client:
            for kind in ("video", "note"):
                try:
                    resp = client.get(
                        f"https://www.iesdouyin.com/share/{kind}/{item_id}/")
                    text = resp.text
                except httpx.HTTPError:
                    continue
                if "window._ROUTER_DATA" in text:
                    m = re.search(
                        r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>",
                        text, re.S)
                    if m:
                        try:
                            return json.loads(m.group(1))
                        except json.JSONDecodeError:
                            return None
        return None

    # ------------------------------------------------------------------
    # 直播流解析
    # ------------------------------------------------------------------

    def get_live_info(self, room_url: str) -> Optional[LiveInfo]:
        cookie = self.cookies.get_header(self.name)
        try:
            with self._client(cookie, mobile=False) as client:
                resp = client.get(room_url)
                resp.raise_for_status()
                html = resp.text
                final_url = str(resp.url)
        except httpx.HTTPError as exc:
            logger.error("获取抖音直播间失败: %s", exc)
            return None

        data = self._parse_render_data(html)
        if data is None:
            data = self._parse_router_data(html)
        if data is None:
            logger.warning("直播间页面未包含流数据（可能未开播或页面结构变更）")
            return LiveInfo(platform=self.name, room_url=room_url,
                            is_live=False, referer=final_url)

        room = _deep_find(data, "room")
        if not isinstance(room, dict):
            return LiveInfo(platform=self.name, room_url=room_url, is_live=False,
                            referer=final_url)

        status = room.get("status")           # 2 = 直播中
        owner = room.get("owner") or {}
        info = LiveInfo(
            platform=self.name,
            room_url=room_url,
            is_live=(status == 2),
            title=room.get("title") or "",
            anchor=owner.get("nickname") or "",
            referer=final_url,
            user_agent=UA_PC,
            cookie=cookie,
        )
        if not info.is_live:
            return info

        # 流地址：stream_url 节点下优先 flv，其次 hls
        stream = room.get("stream_url") or {}
        candidates: List[str] = []
        for key in ("flv_pull_url", "flv_pull_url_FULL_HD1", "hls_pull_url_map",
                    "hls_pull_url", "default_resolution"):
            value = stream.get(key)
            if isinstance(value, dict):
                candidates.extend(v for v in value.values()
                                  if isinstance(v, str) and v.startswith("http"))
            elif isinstance(value, str) and value.startswith("http"):
                candidates.append(value)
        flv = next((u for u in candidates if ".flv" in u), "")
        hls = next((u for u in candidates if ".m3u8" in u), "")
        info.stream_url = flv or hls
        if not info.stream_url:
            logger.warning("已开播但未解析到流地址，页面结构可能已变更")
        return info

    @staticmethod
    def _parse_render_data(html: str) -> Optional[Dict]:
        """抖音页面把数据 URL 编码后放在 <script id="RENDER_DATA"> 里。"""
        m = re.search(r'<script id="RENDER_DATA" type="application/json">'
                      r'(.*?)</script>', html, re.S)
        if not m:
            return None
        try:
            return json.loads(urllib.parse.unquote(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _parse_router_data(html: str) -> Optional[Dict]:
        m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>",
                      html, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None


# ----------------------------------------------------------------------
# 纯函数（可离线单测）
# ----------------------------------------------------------------------

def find_item(data: Any) -> Optional[Dict]:
    """在 _ROUTER_DATA 里广度优先找第一个非空 item_list[0]。"""
    queue = [data]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            item_list = cur.get("item_list")
            if isinstance(item_list, list) and item_list \
                    and isinstance(item_list[0], dict):
                return item_list[0]
            queue.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            queue.extend(v for v in cur if isinstance(v, (dict, list)))
    return None


def pick_image_urls(item: Dict) -> List[str]:
    """从图文条目提取无水印原图。

    url_list 是干净原图；download_url_list 带「抖音号」水印，仅兜底使用。
    """
    urls: List[str] = []
    for image in item.get("images") or []:
        if not isinstance(image, dict):
            continue

        def clean(seq):
            seq = [u for u in (seq or [])
                   if isinstance(u, str) and u.startswith("http")]
            return [u for u in seq if "water" not in u.lower()]

        pool = clean(image.get("url_list")) or \
            clean(image.get("download_url_list"))
        if not pool:
            continue
        pool.sort(key=_image_score, reverse=True)
        urls.append(pool[0])
    return urls


def _image_score(url: str) -> float:
    """图片地址打分：越接近原图分越高。

    「无 ~tplv 处理参数」是原图的决定性特征（+1000），
    分辨率数字只作同档内的次要参考（缩为十分之一，避免喧宾夺主）。
    """
    score = 0.0
    if "~tplv" not in url:
        score += 1000
    elif "resize" not in url:
        score += 500
    nums = [int(n) for n in re.findall(r":(\d{2,4}):", url)]
    if nums:
        score += max(nums) / 10
    if re.search(r"(?i)\.jpe?g", url):
        score += 50
    elif ".webp" in url.lower():
        score -= 10
    return score


def _deep_find(data: Any, key: str) -> Any:
    """在嵌套结构里深度优先找第一个名为 key 的字段。"""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for value in data.values():
            found = _deep_find(value, key)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _deep_find(value, key)
            if found is not None:
                return found
    return None
