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
from media_toolkit.platforms.douyin_sign import ab_sign

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
        """查询直播间状态与流地址。

        抖音 Web 端已改为接口下发数据：房间页 HTML 只含 AB 测试配置，
        必须请求 ``webcast/room/web/enter`` 接口，且查询串要带 a_bogus 签名，
        否则接口返回空响应（风控）。签名实现见 ``douyin_sign``。

        流程：链接 → web_rid → 签名请求接口 → 取 room.status 与 stream_url。
        """
        cookie = self.cookies.get_header(self.name)
        web_rid, referer = self._resolve_web_rid(room_url, cookie)
        if not web_rid:
            logger.error("无法从链接解析出抖音直播间号: %s", room_url)
            return None

        payload = self._fetch_room_api(web_rid, referer, cookie)
        if payload is None:
            # 风控或网络异常，无法判断状态，交由上层按「未开播」处理并稍后重试
            return LiveInfo(platform=self.name, room_url=room_url,
                            is_live=False, referer=referer,
                            user_agent=UA_PC, cookie=cookie)

        rooms = _room_list(payload)
        room = rooms[0] if rooms else {}
        status = room.get("status")            # 2 = 直播中
        anchor = _anchor_name(payload, room)
        info = LiveInfo(
            platform=self.name,
            room_url=room_url,
            is_live=(status == 2),
            title=room.get("title") or "",
            anchor=anchor,
            referer=referer,
            user_agent=UA_PC,
            cookie=cookie,
        )
        if not info.is_live:
            logger.debug("[douyin] 房间 %s 未开播（status=%s）", web_rid, status)
            return info

        info.stream_url = self._pick_stream_url(room)
        if not info.stream_url:
            logger.warning("[douyin] 已开播但未取到流地址（可能是网页端不支持的类型，"
                           "如 VR/特殊玩法）")
        return info

    # ---- 直播辅助 ----

    @staticmethod
    def _extract_web_rid(url: str) -> str:
        """从各种抖音直播链接里取 web_rid。

        已知形态（末尾数字就是 web_rid）：
        - https://live.douyin.com/311923254809
        - https://www.douyin.com/follow/live/311923254809?anchor_id=...
        - https://www.douyin.com/root/live/311923254809
        先只看 path，避免误匹配 query 里的 anchor_id。
        """
        if "douyin.com" not in url:
            return ""
        path = urllib.parse.urlparse(url).path if "://" in url else url
        m = re.search(r"/(\d{6,})/?$", path)
        if m:
            return m.group(1)
        m = re.search(r"live\.douyin\.com/(\d{6,})", url)
        return m.group(1) if m else ""

    def _resolve_web_rid(self, room_url: str, cookie: str) -> tuple:
        """直播链接 → (web_rid, referer)。短链先跟随跳转。"""
        rid = self._extract_web_rid(room_url)
        if rid:
            return rid, f"https://live.douyin.com/{rid}"
        try:
            final_url = self._follow_redirect(room_url, cookie)
        except httpx.HTTPError as exc:
            logger.error("抖音直播短链跳转失败: %s", exc)
            return "", room_url
        rid = self._extract_web_rid(final_url)
        return rid, final_url

    def _fetch_room_api(self, web_rid: str, referer: str,
                        cookie: str) -> Optional[Dict]:
        """带 a_bogus 签名请求房间信息接口。"""
        params = {
            "aid": "6383",
            "app_name": "douyin_web",
            "live_id": "1",
            "device_platform": "web",
            "language": "zh-CN",
            "enter_from": "web_live",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "131.0.0.0",
            "web_rid": web_rid,
        }
        api = ("https://live.douyin.com/webcast/room/web/enter/?"
               + urllib.parse.urlencode(params))
        query = urllib.parse.urlparse(api).query
        api = f"{api}&a_bogus={ab_sign(query, UA_PC)}"

        headers = {
            "User-Agent": UA_PC,
            "Cookie": cookie,
            "Referer": referer,
        }
        try:
            with httpx.Client(timeout=20, headers=headers,
                              proxy=self.proxy or None) as client:
                resp = client.get(api)
            if not resp.text:
                logger.warning("[douyin] 房间接口返回空响应（触发风控），"
                               "签名可能已过期，需同步上游实现")
                return None
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("[douyin] 房间接口请求失败: %s", exc)
            return None

        if data.get("status_code") not in (0, None):
            logger.warning("[douyin] 房间接口返回异常: %s %s",
                           data.get("status_code"), data.get("status_msg"))
            return None
        return _normalize_room_payload(data.get("data"))

    @staticmethod
    def _pick_stream_url(room: Dict) -> str:
        """从 room["stream_url"] 中挑一条可直连的流地址。

        优先原画（ORIGIN 的 flv），其次各档位 flv，最后 hls。
        """
        stream = room.get("stream_url") or {}
        candidates = _origin_candidates(stream)
        for key in ("flv_pull_url", "hls_pull_url_map"):
            value = stream.get(key)
            if isinstance(value, dict):
                candidates.extend(v for v in value.values()
                                  if isinstance(v, str) and v.startswith("http"))
            elif isinstance(value, str):
                candidates.append(value)
        if stream.get("hls_pull_url"):
            candidates.append(stream["hls_pull_url"])

        flv = next((u for u in candidates if ".flv" in u), "")
        hls = next((u for u in candidates if ".m3u8" in u), "")
        return flv or hls


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

def _normalize_room_payload(payload: Any) -> Dict:
    """把房间接口返回统一成 ``{"data": [room, ...], "user": {...}}``。

    实测这个接口的层级并不稳定，见过两种形态：

    - 扁平：``{"data": [room, ...], "user": {...}}``
    - 嵌套：``{"data": {"data": [room], "user": {...}}, ...}``

    统一之后再取房间列表，避免因层级变化而"看起来解析成功、其实是空字典"。
    """
    if isinstance(payload, list):
        return {"data": payload}
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("data")
    if isinstance(inner, dict) and ("data" in inner or "user" in inner):
        merged = dict(inner)
        if not merged.get("user") and isinstance(payload.get("user"), dict):
            merged["user"] = payload["user"]
        return merged
    if isinstance(inner, list):
        return {**payload, "data": inner}
    return payload


def _room_list(payload: Dict) -> List[Dict]:
    """取房间列表，兼容 data 是列表或被再包一层的情况。"""
    rooms = payload.get("data")
    if isinstance(rooms, dict):
        rooms = rooms.get("data")
    if not isinstance(rooms, list):
        return []
    return [r for r in rooms if isinstance(r, dict)]


def _anchor_name(payload: Dict, room: Dict) -> str:
    """主播昵称：优先接口顶层的 user，其次房间对象里的 owner。"""
    user = payload.get("user") or {}
    if isinstance(user, dict) and user.get("nickname"):
        return user["nickname"]
    owner = (room or {}).get("owner") or {}
    return owner.get("nickname") or ""


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


def _origin_candidates(stream: Dict) -> List[str]:
    """解析原画地址。

    抖音把各档位地址放在 ``stream_url.flv_pull_url`` / ``hls_pull_url_map``，
    但原画（ORIGIN）只以 JSON 字符串形式藏在 ``live_core_sdk_data.pull_data``
    或 ``pull_datas[*].stream_data`` 里，需要二次解析。
    """
    blobs: List[str] = []
    pull_datas = stream.get("pull_datas") or {}
    if isinstance(pull_datas, dict) and pull_datas:
        first = pull_datas[next(iter(pull_datas))] or {}
        blobs.append(first.get("stream_data") or "")
    core = stream.get("live_core_sdk_data") or {}
    blobs.append((core.get("pull_data") or {}).get("stream_data") or "")

    urls: List[str] = []
    for blob in blobs:
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        origin = ((data.get("data") or {}).get("origin") or {}).get("main") or {}
        if not isinstance(origin, dict):
            continue
        codec = ""
        try:
            codec = (json.loads(origin.get("sdk_params") or "{}")
                     .get("VCodec") or "")
        except (json.JSONDecodeError, TypeError, AttributeError):
            codec = ""
        for kind in ("flv", "hls"):
            url = origin.get(kind)
            if isinstance(url, str) and url.startswith("http"):
                urls.append(f"{url}&codec={codec}" if codec else url)
    return urls
