"""从汽水音乐分享链接采集音频与封面（免登录版）。

设计取舍（见 2026-11-21 工作日志）：
- 汽水音乐已经没有网页播放器，``music.douyin.com`` 是音乐人合作平台；
  「我的收藏 / 站内搜索」必须登录且要 PC 签名头，免登录拿不到。
- 可免登录拿到的是**分享页**：单曲页 ``douyin.com/qishui/song/<id>`` 和
  歌单短链 ``qishui.douyin.com/s/<code>`` 会在 HTML 里内联 ``_ROUTER_DATA``，
  其中包含音频直链、封面、标题、歌手、歌词，解析它比调接口稳。
- 免登录的音频是**试听片段**（约 30 秒，m4a），不是完整曲目。完整版要抓包
  凭证（Cookie + x-helios/x-medusa），留到以后；界面上会明确标注「试听」。
- 视频不在这里采——用户选的是复用现有抖音导入管线。

对外主要入口：
    - ``parse_share(text)``      从分享文本里识别单曲/歌单链接或纯 id
    - ``collect(item, on_item)`` 采集并返回曲目列表
    - ``download(track, out_dir)`` 下载音频与封面到本地
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dly.core.naming import build_filename, safe_name

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_API = "https://api.qishui.com"
_SONG_PAGE = "https://www.douyin.com/qishui/song/{id}"
_PLAYLIST_API = _API + "/luna/playlist/detail?playlist_id={pid}"
_QRCODE_API = _API + "/passport/web/get_qrcode/"
_QRSTATUS_API = _API + "/passport/web/check_qrconnect/"
_ME_PLAYLISTS_API = _API + "/luna/pc/me/playlist"
# 抖音 passport 固定参数（从汽水 PC 客户端抓包得到，开源实现同款）
_AID = "386088"
_IID = "27960026095955"
_VERSION_CODE = "30020100"
_PASSPORT_FIXED = {
    "passport_jssdk_version": "2.4.13",
    "passport_jssdk_type": "normal",
    "is_from_ttaccountsdk": "1",
    "aid": _AID,
    "next": _API,
    "need_logo": "false",
    "need_short_url": "false",
    "is_new_login": "1",
}

# 只接受汽水/抖音域名，防止分享文本里夹带别的链接被当成资源去请求
# douyinvod.com / douyinpic.com 是音频与封面的 CDN，必须一起放行
_ALLOWED_HOSTS = ("douyin.com", "qishui.com", "bytedance.com",
                  "douyinvod.com", "douyinpic.com", "bytescm.com")
_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff]+", re.I)
_SONG_RE = re.compile(r"/qishui/(?:song|track)/(\d+)")
_PLAYLIST_RE = re.compile(r"[?&]playlist_id=(\d+)")
_TRACK_ID_RE = re.compile(r"[?&]track_id=(\d+)")
_SHORT_RE = re.compile(r"qishui\.douyin\.com/s/([A-Za-z0-9_-]+)")
_VO_PAGE_RE = re.compile(r"[?&]modal_id=(\d+)")


@dataclass
class QishuiTrack:
    """一条可下载的汽水曲目（免登录时为试听片段）。"""
    id: str
    title: str
    artist: str = ""
    album: str = ""
    cover_url: str = ""
    audio_url: str = ""
    duration: float = 0.0
    preview: bool = True
    lyric: str = ""
    source_url: str = ""
    error: str = ""


@dataclass
class QishuiCollection:
    """一次分享链接解析的结果。"""
    kind: str                       # "track" | "playlist"
    title: str
    tracks: List[QishuiTrack] = field(default_factory=list)
    note: str = ""


def build_lrc(lyric: str, duration: float = 0) -> str:
    """把解析出的歌词整理成标准 LRC 文本。

    汽水句子带 ``[mm:ss.cc]`` 前缀；没有时间轴的（例如纯文本）直接保留。
    """
    lines = [ln for ln in (lyric or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    out = ["[by:DLY 汽水音乐采集]"]
    if duration:
        total = int(duration)
        out.append(f"[length:{total // 60:02d}:{total % 60:02d}]")
    out.extend(lines)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------

def _allowed(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS)


def _http(url: str, timeout: float = 25.0, referer: str = "") -> bytes:
    if not _allowed(url):
        raise ValueError("链接不是汽水/抖音域名，已拒绝请求")
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _request(url: str, data: Optional[bytes] = None, headers: Optional[dict] = None,
             timeout: float = 25.0):
    """返回 (body, headers)。扫码轮询要从 Set-Cookie 里取 sessionid。"""
    if not _allowed(url):
        raise ValueError("链接不是汽水/抖音域名，已拒绝请求")
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers


def _session_cookie(sessionid: str) -> str:
    """校验并拼出 Cookie 头；只允许安全字符，避免注入。"""
    sid = str(sessionid or "").strip()
    if not sid or re.search(r"[;\s\x00-\x1F\x7F]", sid):
        raise ValueError("登录态无效，请重新扫码")
    return f"sessionid={sid};"


def auth_qrcode() -> Dict[str, Any]:
    """获取扫码登录二维码。返回 {token, qrcode(data:image...), expire_at}。"""
    query = urllib.parse.urlencode(_PASSPORT_FIXED)
    body, headers = _request(_QRCODE_API + "?" + query)
    data = json.loads(body).get("data") or {}
    token = str(data.get("token") or "")
    qrcode = str(data.get("qrcode") or "")
    if not token or not qrcode:
        raise ValueError("获取二维码失败，请稍后重试")
    # 拿二维码时服务端会给一个临时 cookie，轮询要带着它
    cookies = headers.get_all("Set-Cookie") or []
    cookie_header = "; ".join(c.split(";", 1)[0] for c in cookies)
    return {"token": token, "qrcode": qrcode,
            "expire_at": int(data.get("expire_time") or 0),
            "cookie": cookie_header}


def auth_qrcode_status(token: str, cookie: str = "") -> Dict[str, Any]:
    """轮询扫码状态。已确认时从 Set-Cookie 里取 sessionid。"""
    if not token:
        raise ValueError("扫码 token 为空")
    query = urllib.parse.urlencode({
        "passport_jssdk_version": _PASSPORT_FIXED["passport_jssdk_version"],
        "passport_jssdk_type": _PASSPORT_FIXED["passport_jssdk_type"],
        "is_from_ttaccountsdk": _PASSPORT_FIXED["is_from_ttaccountsdk"],
        "aid": _AID, "iid": _IID,
        "need_logo": "false", "need_short_url": "false",
        "is_frontier": "true", "token": token, "is_new_login": "1",
        "next": _API,
    })
    form = urllib.parse.urlencode({
        "need_logo": "false", "need_short_url": "false",
        "is_frontier": "true", "token": token, "is_new_login": "1",
        "next": _API,
    }).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    body, resp_headers = _request(_QRSTATUS_API + "?" + query, data=form,
                                  headers=headers)
    data = json.loads(body).get("data") or {}
    # 登录凭证可能叫 sessionid / sessionid_ss / sid_tt / sid_guard，
    # 也可能直接出现在响应体里；都要认，不然「扫码成功却没反应」。
    sessionid = ""
    names = ("sessionid", "sessionid_ss", "sid_tt", "sid_guard")
    for value in resp_headers.get_all("Set-Cookie") or []:
        for name in names:
            m = re.search(r"(?:^|[;,]\s*)" + name + r"=([^;,\s]+)", value, re.I)
            if m:
                sessionid = m.group(1)
                break
        if sessionid:
            break
    if not sessionid:
        for name in names:
            v = data.get(name)
            if v:
                sessionid = str(v)
                break
    status = str(data.get("status") or "")
    # 有的实现确认后只给 redirect_url，登录 cookie 要访问它才下发
    redirect = str(data.get("redirect_url") or "")
    if not sessionid and redirect and _allowed(redirect):
        try:
            _, redirect_headers = _request(redirect, headers={"Cookie": cookie})
            for value in redirect_headers.get_all("Set-Cookie") or []:
                for name in names:
                    m = re.search(r"(?:^|[;,]\s*)" + name + r"=([^;,\s]+)",
                                  value, re.I)
                    if m:
                        sessionid = m.group(1)
                        break
                if sessionid:
                    break
        except Exception as exc:
            logger.debug("扫码跳转取登录态失败: %s", exc)
    return {"status": status,
            "sessionid": sessionid,
            "error_code": int(data.get("error_code") or 0),
            "has_cookie": bool(resp_headers.get_all("Set-Cookie"))}


def me_playlists(sessionid: str) -> List[Dict[str, str]]:
    """登录态下拉我的歌单列表，返回 [{id, title, count}]。"""
    query = urllib.parse.urlencode({"aid": _AID, "iid": _IID,
                                    "version_code": _VERSION_CODE})
    headers = {"User-Agent": "LunaPC/3.0.0(290101097)",
               "Cookie": _session_cookie(sessionid)}
    body, _ = _request(_ME_PLAYLISTS_API + "?" + query, headers=headers)
    data = json.loads(body) or {}
    out = []
    for p in data.get("playlists") or []:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        out.append({"id": pid, "title": str(p.get("title") or "歌单"),
                    "count": str(p.get("count_tracks") or p.get("resource_cnt") or "")})
    return out


def _router_data(html: str) -> dict:
    """从分享页 HTML 里抠出 ``_ROUTER_DATA`` 的平衡 JSON。"""
    marker = html.find("_ROUTER_DATA")
    if marker < 0:
        raise ValueError("分享页里没有 _ROUTER_DATA")
    start = html.find("{", marker)
    if start < 0:
        raise ValueError("_ROUTER_DATA 格式异常")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start:i + 1])
    raise ValueError("_ROUTER_DATA JSON 不完整")


def _page_router(url: str) -> dict:
    html = _http(url).decode("utf-8", "replace")
    return _router_data(html)


# ---------------------------------------------------------------------------
# 链接解析
# ---------------------------------------------------------------------------

def parse_share(text: str) -> Dict[str, str]:
    """从分享文本里识别资源。返回 ``{"kind": ..., "value": ...}``。

    支持：
    - 单曲页 ``https://www.douyin.com/qishui/song/<id>``
    - 纯数字 track id
    - 歌单短链 ``https://qishui.douyin.com/s/<code>``（会跟随跳转到 playlist id）
    - 歌单直链 ``...playlist?playlist_id=<id>``
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("先粘贴汽水音乐的分享链接")

    urls = _URL_RE.findall(raw)
    url = ""
    for candidate in urls:
        candidate = candidate.rstrip(".,;)】」")
        if _allowed(candidate):
            url = candidate
            break

    if url:
        m = _SONG_RE.search(url)
        if m:
            return {"kind": "track", "value": m.group(1)}
        m = _PLAYLIST_RE.search(url)
        if m:
            return {"kind": "playlist", "value": m.group(1)}
        if _SHORT_RE.search(url):
            # 短链要跟随重定向才知道是歌单还是单曲
            return {"kind": "short", "value": url}

    digits = re.findall(r"\b\d{10,}\b", raw)
    if digits:
        return {"kind": "track", "value": digits[0]}
    raise ValueError("没识别出单曲或歌单链接，请复制汽水音乐里的完整分享文本")


def _resolve_short(url: str) -> Dict[str, str]:
    """跟随短链重定向，从最终 URL 里取 playlist_id / song id。"""
    final = url
    for _ in range(5):
        if not _allowed(final):
            raise ValueError("短链跳转到了非汽水/抖音域名")
        req = urllib.request.Request(final, headers={"User-Agent": UA,
                                                     "Accept": "text/html"})
        # 不自动重定向，自己看 Location，避免跑到别的域
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(req, timeout=25)
            body = resp.read().decode("utf-8", "replace")
            data = _router_data(body)
            # 短链最终页的 _ROUTER_DATA 里直接带 playlist_page
            ld = data.get("loaderData", {})
            page = ld.get("playlist_page") or {}
            pid = str(page.get("playlist", {}).get("id") or "")
            if not pid:
                medias = page.get("medias") or []
                if medias:
                    pid = str(medias[0].get("id") or "")
            if pid:
                return {"kind": "playlist", "value": pid}
            # 单曲页的 _ROUTER_DATA 带 track_id
            ld_song = ld.get("song_(id)/page") or {}
            tid = str(ld_song.get("track_id") or "")
            if tid:
                return {"kind": "track", "value": tid}
            # 最终 URL 本身也带 playlist_id / track_id
            m = _PLAYLIST_RE.search(final)
            if m:
                return {"kind": "playlist", "value": m.group(1)}
            m = _TRACK_ID_RE.search(final)
            if m:
                return {"kind": "track", "value": m.group(1)}
            return {"kind": "playlist", "value": ""}
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else ""
            if exc.code in (301, 302, 303, 307, 308) and location:
                final = urllib.parse.urljoin(final, location)
                # 重定向地址里通常就带 playlist_id / track_id，直接用它，不再多打一次页面
                m = _PLAYLIST_RE.search(final)
                if m:
                    return {"kind": "playlist", "value": m.group(1)}
                m = _TRACK_ID_RE.search(final)
                if m:
                    return {"kind": "track", "value": m.group(1)}
                continue
            raise
    raise ValueError("短链跳转次数过多")


# ---------------------------------------------------------------------------
# 曲目提取
# ---------------------------------------------------------------------------

def _cover_of(track: dict) -> str:
    """从专辑 url_cover 结构拼出可直接下载的图片地址。"""
    cover = (track.get("album") or {}).get("url_cover") or track.get("url_cover") or {}
    urls = cover.get("urls") or []
    uri = cover.get("uri") or ""
    if not urls or not uri:
        return ""
    prefix = urls[0] if urls[0].endswith("/") else urls[0] + "/"
    tpl = cover.get("template_prefix") or "tplv-b829550vbb"
    return f"{prefix}{uri}~{tpl}-resize:512:512.image"


def _track_from_option(opt: dict, source_url: str) -> QishuiTrack:
    info = opt.get("trackInfo") or {}
    album = info.get("album") or {}
    artists = info.get("artists") or []
    artist = opt.get("artistName") or ""
    if not artist and artists:
        artist = artists[0].get("name") or artists[0].get("simple_display_name") or ""
    lyrics = opt.get("lyrics") or {}
    lines = []
    for s in lyrics.get("sentences") or []:
        text = s.get("text")
        if not text:
            continue
        start = int(s.get("startMs") or 0)
        if start > 0:
            mm = start // 60000
            ss = (start % 60000) // 1000
            cc = (start % 1000) // 10
            lines.append(f"[{mm:02d}:{ss:02d}.{cc:02d}]{text}")
        else:
            lines.append(text)
    dur = opt.get("duration") or 0
    try:
        duration = float(dur)
    except (TypeError, ValueError):
        duration = 0.0
    return QishuiTrack(
        id=str(opt.get("track_id") or info.get("id") or ""),
        title=str(opt.get("trackName") or info.get("name") or ""),
        artist=str(artist),
        album=str(album.get("name") or ""),
        cover_url=str(opt.get("coverURL") or _cover_of(info) or ""),
        audio_url=str(opt.get("url") or ""),
        duration=duration,
        preview=True,
        lyric="\n".join(lines),
        source_url=source_url,
    )


def _song_page(track_id: str) -> QishuiTrack:
    url = _SONG_PAGE.format(id=track_id)
    data = _page_router(url)
    ld = data.get("loaderData", {})
    page = ld.get("song_(id)/page") or {}
    opt = page.get("audioWithLyricsOption") or {}
    if not opt:
        raise ValueError("单曲页里没有音频数据（可能需要登录或已下架）")
    return _track_from_option(opt, url)


def _playlist_detail(playlist_id: str) -> QishuiCollection:
    data = json.loads(_http(_PLAYLIST_API.format(pid=playlist_id)))
    pl = data.get("playlist") or {}
    tracks: List[QishuiTrack] = []
    for res in data.get("media_resources") or []:
        if res.get("type") != "track":
            continue
        t = (((res.get("entity") or {}).get("track_wrapper") or {}).get("track") or {})
        tid = str(t.get("id") or "")
        if not tid:
            continue
        artists = t.get("artists") or []
        tracks.append(QishuiTrack(
            id=tid,
            title=str(t.get("name") or ""),
            artist=str(artists[0].get("name") if artists else ""),
            album=str((t.get("album") or {}).get("name") or ""),
            cover_url=_cover_of(t),
        ))
    return QishuiCollection(
        kind="playlist",
        title=str(pl.get("title") or "汽水歌单"),
        tracks=tracks,
        note="免登录只能拿到试听片段（约 30 秒）；完整音频需要抓包凭证",
    )


def _fill_audio(track: QishuiTrack, on_progress: Optional[Callable[[str, int, int], None]] = None) -> None:
    """歌单接口不带音频直链，用单曲分享页补齐（仍免登录）。"""
    try:
        full = _song_page(track.id)
        track.audio_url = full.audio_url
        track.cover_url = track.cover_url or full.cover_url
        track.lyric = track.lyric or full.lyric
        if not track.title:
            track.title = full.title
        if not track.artist:
            track.artist = full.artist
        if not track.duration:
            track.duration = full.duration
    except Exception as exc:  # 单首失败不影响整张歌单
        logger.debug("补齐汽水音频失败 %s: %s", track.id, exc)
        track.error = str(exc)[:120]


def collect(parsed: Dict[str, str],
            on_progress: Optional[Callable[[str, int, int], None]] = None) -> QishuiCollection:
    """按 parse_share 的结果采集。on_progress(阶段, 当前, 总数)。"""
    kind, value = parsed.get("kind", ""), parsed.get("value", "")
    if kind == "short":
        parsed = _resolve_short(value)
        if not parsed.get("value"):
            raise ValueError("短链解析失败，请直接粘歌单链接")
        kind, value = parsed["kind"], parsed["value"]

    if kind == "track":
        t = _song_page(value)
        return QishuiCollection(
            kind="track", title=t.title, tracks=[t],
            note="免登录只能拿到试听片段（约 30 秒）",
        )

    if kind == "playlist":
        result = _playlist_detail(value)
        total = len(result.tracks)
        for i, t in enumerate(result.tracks, 1):
            if on_progress:
                on_progress("解析音频", i, total)
            _fill_audio(t, on_progress)
        return result

    raise ValueError("不支持的分享类型")


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

@dataclass
class Downloaded:
    audio: Path
    cover: Optional[Path]
    track: QishuiTrack


def download(track: QishuiTrack, out_dir: Path,
             on_progress: Optional[Callable[[str, int, int], None]] = None) -> Downloaded:
    """下载音频和封面到 out_dir。音频扩展名用 .m4a（汽水返回的是 M4A 容器）。"""
    if not track.audio_url:
        raise ValueError("这条没有可下载的音频直链")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    audio_path = out_dir / build_filename(
        "汽水音乐", track.artist, track.title, ext=".m4a")

    data = _http(track.audio_url, timeout=60, referer="https://www.douyin.com/")
    if len(data) < 1024:
        raise ValueError("音频文件太小，可能已经失效")
    if not data[4:12].startswith(b"ftyp"):
        raise ValueError("下载到的不是 M4A 音频")
    audio_path.write_bytes(data)

    cover_path = None
    if track.cover_url:
        try:
            img = _http(track.cover_url, timeout=25,
                        referer="https://www.douyin.com/")
            if img[:2] == b"\xff\xd8":
                cover_path = audio_path.with_suffix(".jpg")
                cover_path.write_bytes(img)
        except Exception as exc:
            logger.debug("汽水封面下载失败 %s: %s", track.id, exc)
    return Downloaded(audio=audio_path, cover=cover_path, track=track)
