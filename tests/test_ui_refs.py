"""界面脚本引用完整性：JS 取用的每个元素 id 都必须真实存在于 HTML 里。

背景：精简侧栏时删掉了 `#conn` / `#ffmpeg-badge` / `#quit-btn`，但脚本里还在
`$('conn').textContent = ...`。结果第一次轮询就在 try 里抛异常、进 catch 又抛，
`setTimeout(poll)` 再也执行不到——界面从此永不刷新（下载记录、铃铛红点、进度条
全部不动，只有重启程序从磁盘读回才看得见）。这类「引用了已删元素」的错只能在
浏览器里运行时才炸，静态测试守不住，所以这里做一次 id 对账。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

HTML_PATH = (Path(__file__).resolve().parents[1]
             / "src" / "dly" / "gui" / "assets" / "index.html")


@pytest.fixture(scope="module")
def html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _used_ids(html: str) -> set[str]:
    """脚本里通过 $('id') / getElementById('id') 取用的元素。"""
    ids = set(re.findall(r"\$\('([^']+)'\)", html))
    ids |= set(re.findall(r"getElementById\('([^']+)'\)", html))
    return ids


def _defined_ids(html: str) -> set[str]:
    """HTML 里静态写死的 id（脚本动态生成的节点另算）。"""
    return set(re.findall(r'id="([^"]+)"', html))


def test_no_dangling_element_ids(html):
    """不允许引用 HTML 里不存在的 id——那会让 render/poll 直接抛异常。"""
    missing = sorted(_used_ids(html) - _defined_ids(html))
    assert not missing, (
        "JS 引用了 HTML 里不存在的元素 id：" + ", ".join(missing)
        + "（删元素时要同步删脚本里的引用，或者加空值判断）")


def test_poll_keeps_running_when_render_fails(html):
    """render 抛异常也必须继续安排下一次轮询，否则界面会永久停更。"""
    body = html.split("async function poll()", 1)[1].split("\n}", 1)[0]
    assert "try { render(); }" in body, "render() 要单独包一层 try/catch"
    # setTimeout 必须在 try/catch 之外，保证任何异常都还能续上
    assert body.count("setTimeout(poll") == 1
    assert body.index("setTimeout(poll") > body.index("catch")


def test_bell_badge_not_hidden_by_compact_sidebar(html):
    """紧凑侧栏那条 `display:none` 不能连角标一起藏掉。"""
    rule = re.search(r"body\.sidebar-compact[^{]*\{[^}]*display: none", html)
    assert rule, "紧凑侧栏的隐藏规则没找到"
    assert ".side-bell span:not(.bell-badge)" in html


def test_keep_original_only_shown_when_file_will_change(html):
    """「保留原素材」只在会动原文件时出现：原帧率 + 最佳画质时整项藏起来。"""
    assert 'id="dl-keep-wrap" hidden' in html
    assert 'id="dl-keep"' in html
    # 两个下拉变化都要重新判断显隐
    assert html.count('onchange="syncKeepOriginal()"') == 2
    # 显隐规则：帧率为 0（原帧率）且画质 best（原画质）才隐藏
    body = html.split("function syncKeepOriginal()", 1)[1].split("\n}", 1)[0]
    assert "fpsOf('dl-fps') <= 0" in body and "quality === 'best'" in body
    # label 用了 inline-flex，hidden 属性需要 !important 才能压住
    assert ".check[hidden] { display: none !important; }" in html


def test_tunnel_view_is_wired(html):
    """歌单页要有隧道视图入口，模块与内置 three 都要在包里。"""
    base = HTML_PATH.parent
    module = (base / "music-tunnel.js").read_text(encoding="utf-8")
    assert '<script type="module" src="music-tunnel.js"></script>' in html
    assert "window.MusicTunnel = " in module
    assert "from './three/three.module.min.js'" in module
    for name in ("three/three.module.min.js", "three/three.core.min.js"):
        assert (base / name).is_file(), name
    # 指针捕获必须设在画布上，否则 pointerup 被 host 吃掉、点击永远不触发
    assert "renderer.domElement.setPointerCapture" in module
    assert "host.setPointerCapture" not in module
    assert 'id="music-tunnel"' in html
    # addon 里写的是 `from 'three'`，靠 import map 指到本地包
    assert '"three": "/three/three.module.min.js"' in html
    assert "EffectComposer" in module and "UnrealBloomPass" in module
    # addon 里写的是 `from 'three'`，靠 import map 指到本地包
    assert '"three": "/three/three.module.min.js"' in html
    assert "EffectComposer" in module and "UnrealBloomPass" in module
    assert "toggleMusicView()" in html


def test_tunnel_view_is_wired(html):
    """歌单页要有隧道视图入口，模块与内置 three 都要在包里。"""
    base = HTML_PATH.parent
    module = (base / "music-tunnel.js").read_text(encoding="utf-8")
    assert '<script type="module" src="music-tunnel.js"></script>' in html
    assert "window.MusicTunnel = " in module
    assert "from './three/three.module.min.js'" in module
    for name in ("three/three.module.min.js", "three/three.core.min.js"):
        assert (base / name).is_file(), name
    # 指针捕获必须设在画布上，否则 pointerup 被 host 吃掉、点击永远不触发
    assert "renderer.domElement.setPointerCapture" in module
    assert "host.setPointerCapture" not in module
    assert 'id="music-tunnel"' in html
    # addon 里写的是 `from 'three'`，靠 import map 指到本地包
    assert '"three": "/three/three.module.min.js"' in html
    assert "EffectComposer" in module and "UnrealBloomPass" in module
    # addon 里写的是 `from 'three'`，靠 import map 指到本地包
    assert '"three": "/three/three.module.min.js"' in html
    assert "EffectComposer" in module and "UnrealBloomPass" in module
    assert "toggleMusicView()" in html


def test_tunnel_click_can_skip_switching_track(html):
    """隧道里点封面可以只换主封面不切歌：开关要存在，且 onSelect 里要有这个分支。"""
    assert 'id="opt-tunnel-play"' in html
    assert "function setTunnelPlays(" in html
    assert "if (!tunnelPlays) return;" in html
    # 开关状态存在本地，刷新后保持
    assert "localStorage.getItem('dly.tunnelPlays')" in html
    assert "localStorage.setItem('dly.tunnelPlays'" in html


def test_only_titlebar_drags_window(html):
    """pywebview 的 easy_drag 监听 window 的 mousedown，整页都能拖窗口。
    现在反过来：只有标题栏空白处放行，其余一律截断（选文字、拖滑块、拖隧道都不再挪窗口）。"""
    assert "const inTitlebar = !!target.closest('.app-titlebar');" in html
    assert "if (!inTitlebar || interactive) e.stopPropagation();" in html
    # 必须用冒泡阶段且注册在最后，否则会打断「点外面关弹层」
    assert "});   // 用冒泡阶段" in html


def test_playback_modes_are_explicit(html):
    """三种播放模式要能直接选：顺序播放 / 单曲循环 / 随机播放。"""
    assert 'id="pl-mode"' in html
    for label in ("顺序播放", "单曲循环", "随机播放"):
        assert label in html
    assert "function setPlayMode(" in html
    # 顺序播放放完最后一首要停，而不是绕回第一首
    assert "auto && player.mode === 'list' && index === list.length - 1" in html
    assert "localStorage.setItem('dly.playMode'" in html
    # 图标按钮：点一下切换，图标随模式换
    assert 'data-mode="list"' in html and 'data-mode="single"' in html
    assert "function cyclePlayMode(" in html


def test_extract_audio_offers_cover_pick(html):
    """「从视频提取」完成后要直接把挑封面弹出来，而不是让用户再去找换封面。"""
    assert "openCoverPicker(data.track.id, true)" in html
    assert "提取完成，默认用的是视频第一秒。" in html
    assert "function openCoverPicker(id, afterExtract)" in html


def test_switching_track_starts_from_zero(html):
    """切换歌曲必须从头播放；点当前这首则不重新加载，只切换播放/暂停。"""
    assert "if (player.id === id && audio.src) {" in html
    assert "togglePlay();" in html
    # 换歌不再用 last_pos 恢复位置
    assert "const resume = Math.max(0, track.last_pos || 0);" not in html
    # 切换瞬间的 timeupdate 带着上一首的 currentTime，不能写进新曲目的进度
    assert "if (player.switching) return;" in html
    assert "if (!player.id || player.switching) return;" in html


def test_center_cover_single_pause_double_replay(html):
    """中央封面：单击暂停/继续，双击从头播放。自己数连击，别依赖原生 dblclick。"""
    assert "function handleTunnelSelect(id, isHero)" in html
    assert "if (now - lastHeroClick < 320)" in html
    assert "audio.currentTime = 0;" in html
    assert "tunnelClickTimer = setTimeout(() => { togglePlay(); }, 300);" in html
    # 主封面要能被点中（早前被排除在拾取之外，点正中间毫无反应）
    module = (HTML_PATH.parent / "music-tunnel.js").read_text(encoding="utf-8")
    assert "if (slot === state.heroSlot) {" in module


def test_same_song_other_cover_does_not_toggle(html):
    """同一首歌在隧道里有好几张封面：点不是中央那张，只换主封面，不能暂停。"""
    assert "function handleTunnelSelect(id, isHero)" in html
    assert "if (!isHero) return;" in html
    module = (HTML_PATH.parent / "music-tunnel.js").read_text(encoding="utf-8")
    assert "state.onSelect(track.id, { hero: true })" in module
    assert "state.onSelect(track.id, { hero: false })" in module


def test_music_toolbar_moved_to_header(html):
    """歌单工具条搬到页头右侧；Cookie 三个控件收进「⋯」；说明进「?」弹层。"""
    assert 'id="music-actions"' in html
    assert 'id="music-help-btn"' in html
    assert 'id="music-help-pop"' in html
    assert 'id="music-more-pop"' in html
    assert 'id="music-browser"' in html
    # 卡片里不该再有工具条和说明行
    assert 'class="music-head"' not in html
    assert 'music-tunnel-tip' not in html
    # 弹层要显式 absolute，否则 top:100% 会算成整屏高度跑到屏幕外
    assert "position: absolute;\n    left: 0; right: auto;" in html


def test_player_floats_as_glass_in_tunnel(html):
    """隧道视图里播放条浮在画面底部，做成液态玻璃；切回网格恢复普通卡片。"""
    assert ".player.floating {" in html
    assert "backdrop-filter: blur(22px) saturate(170%);" in html
    assert "classList.toggle('floating', musicView === 'tunnel' && hasTracks)" in html
    # 这句挪进 ? 说明里：页面标题下不再显示，问号弹层的 fallback 里要有
    assert "music:    ['歌单', '']," in html
    assert "sub || '下载或提取的音频都在这里，点封面就能听'" in html


def test_floating_player_can_be_hidden(html):
    """隧道里的玻璃播放条可以收起，收起后右下角留一个圆钮能调回来。"""
    assert 'id="pl-hide"' in html and 'id="pl-restore"' in html
    assert "function togglePlayerHidden(" in html
    assert "function applyPlayerHidden(" in html
    assert ".player.collapsed { display: none !important; }" in html
    # 收起状态只在隧道视图生效，网格视图照常显示
    assert "const collapsed = floating && playerHidden;" in html
    assert "localStorage.setItem('dly.playerHidden'" in html
    # 空副标题不留空行
    assert "subEl.hidden = !meta[1];" in html
    # 隐藏/恢复两条绿色提示都只在第一次出现
    assert "const key = playerHidden ? 'dly.playerHideTipShown' : 'dly.playerShowTipShown';" in html
    assert "const msg = playerHidden ? '播放条已隐藏，右下角小圆钮可以调回来' : '播放条已显示';" in html
    assert "localStorage.getItem(key) === '1'" in html
    assert "localStorage.setItem(key, '1')" in html
    assert "if (!tipShown) {" in html
    # 恢复按钮 hover 用不透明深色压过通用 button:hover 的浅色背景，避免整颗钮全白
    assert "button.player-restore:hover {" in html
    assert "background: linear-gradient(180deg, #3a3457, #242033);" in html
    assert "button:hover:not(:disabled) { border-color: var(--btn-hover-line)" in html
    # svg 不拦截指针，hover/click 落在按钮本身
    assert ".player-restore svg { width: 18px; height: 18px; stroke: currentColor; pointer-events: none; }" in html


def test_tunnel_recycles_slots_across_spans_on_fast_scroll(html):
    """一次滚很远/缩放时，被甩到身后的封面要按跨越的档数一次回收到位，
    否则前方出现空洞、没有新封面补出来；主封面让路的判定也要有上限，
    不能把整条隧道都淡出。"""
    module = (HTML_PATH.parent / "music-tunnel.js").read_text(encoding="utf-8")
    # 回收按 (slot.z - camZ - 1) / (LAYERS * D) 的跨度倍数搬，而不是只挪一档
    assert "const spans = Math.ceil((slot.z - camZ - 1) / (LAYERS * D));" in module
    assert "slot.z -= spans * LAYERS * D;" in module
    # 主封面让路只作用到紧邻几层（heroFrontZ + 3 * D），避免远处的层全被藏掉
    assert "slotZ < heroFrontZ + 3 * D" in module
    # 滚轮 delta 夹幅，防止一帧跳几十单位
    assert "Math.max(-120, Math.min(120, event.deltaY))" in module


def test_spotify_skin_is_scoped_and_reversible(html):
    """Spotify 皮肤是可选、可逆的：一个开关，样式只作用于歌单网格，隧道不受影响。"""
    # 开关接线与状态记忆
    assert 'id="music-skin-btn"' in html
    assert "onclick=\"toggleMusicSkin()\"" in html
    assert "function toggleMusicSkin(" in html
    assert "document.body.classList.toggle('spotify-skin', spotifySkin)" in html
    assert "localStorage.getItem('dly.spotifySkin')" in html
    assert "localStorage.setItem('dly.spotifySkin'" in html
    # 默认开启（歌单整页走 Spotify 深色）：初始 true，且只有明确存 '0' 才关
    assert "let spotifySkin = true;" in html
    assert "spotifySkin = savedSkin !== '0';" in html
    # 作用域：所有皮肤规则都带 spotify-skin + #panel-music，且播放条排除浮动态
    assert "body.spotify-skin #panel-music .music-card" in html
    assert "body.spotify-skin #panel-music .player:not(.floating)" in html
    # 绿只做功能色（播放键 / 进度 / 正在播放描边）
    assert "background: #1ed760; color: #000;" in html
    assert "outline-color: #1ed760" in html
    # 隧道视图的液态玻璃定义仍在，没被皮肤覆盖
    assert ".player.floating {" in html and "backdrop-filter: blur(22px) saturate(170%);" in html
    # 发现区也纳入皮肤深色（chips / 快捷入口 / 红心 / 空状态）
    assert "body.spotify-skin #panel-music .filter-chip" in html
    assert "body.spotify-skin #panel-music .fav-btn.on" in html
    assert "body.spotify-skin #panel-music #music-no-match" in html



def test_music_discover_wiring(html):
    """网格视图的发现区：来源筛选 / 快捷入口 / 最近添加 / 本地红心收藏。

    全部用真实曲目数据（added_at/source/封面），不造在线曲库；只作用于网格，
    隧道视图要隐藏；红心只存前端 localStorage，不动后端结构。
    """
    # 发现区容器与三块子区域都在静态 HTML 里（id 对账也能覆盖）
    for eid in ("music-discover", "music-filters", "music-quick", "music-recent"):
        assert f'id="{eid}"' in html, eid
    assert 'id="music-discover" hidden' in html  # 无曲目时默认隐藏
    # 渲染与交互函数
    for fn in ("renderDiscover", "setMusicFilter", "filteredTracks",
               "toggleLiked", "saveLiked", "coverUrl"):
        assert f"function {fn}(" in html, fn
    # 筛选按真实 source 字段与本地收藏集合
    assert "all.filter(t => t.source === musicFilter)" in html
    assert "all.filter(t => likedIds.has(t.id))" in html
    # 最近添加按 added_at 倒序、取前 8 首，且排除缺失曲目
    assert "sort((a, b) => (b.added_at || 0) - (a.added_at || 0))" in html
    assert ".slice(0, 8)" in html
    assert "all.filter(t => !t.missing)" in html
    # 红心持久化用 localStorage，键固定；点击不能冒泡到播放整卡
    assert "localStorage.getItem('dly.likedTracks')" in html
    assert "localStorage.setItem('dly.likedTracks'" in html
    assert "fav.onclick = ev => { ev.stopPropagation(); toggleLiked(t.id); };" in html
    # 快捷入口封面走真实封面接口，随机漫游从可播放曲目里取
    assert "pickCover" in html
    assert "playTrack(usable[Math.floor(Math.random() * usable.length)].id)" in html
    # 隧道视图发现区隐藏（两处：视图切换 + 每次 render 重算，防止切歌时漏出来）
    assert "box.hidden = all.length === 0 || musicView === 'tunnel';" in html
    assert "discover.hidden = musicView === 'tunnel' || !hasTracks;" in html


def test_douyin_import_wiring(html):
    """抖音喜欢/收藏批量导入：下载页入口 + 弹层 + 后端 API 必须都在。"""
    # 下载页入口按钮
    assert 'id="dy-import-btn"' in html
    assert 'onclick="openDyImport()"' in html
    # 弹层与关键交互元素
    for eid in ("dy-modal", "dy-status", "dy-login-btn", "dy-scan",
                "dy-scan-btn", "dy-stop-btn", "dy-list", "dy-footer",
                "dy-download-btn", "dy-select-all"):
        assert f'id="{eid}"' in html, eid
    # 前端必须经过的后端接口
    for api in ("/api/dyimport/check", "/api/dyimport/login",
                "/api/dyimport/scan", "/api/dyimport/stop",
                "/api/dyimport/download"):
        assert api in html, api
    # 导入下载复用下载页的画质/目录设置
    assert "quality: $('dl-quality').value" in html
    assert "output_dir: $('dl-outdir').value.trim()" in html


def test_douyin_import_server_routes():
    server = (HTML_PATH.parents[1] / "server.py").read_text(encoding="utf-8")
    for route in ("/api/dyimport/status", "/api/dyimport/login",
                  "/api/dyimport/check", "/api/dyimport/scan",
                  "/api/dyimport/stop", "/api/dyimport/download"):
        assert f'path == "{route}"' in server, route
    # 导入最终走现有下载线程，而不是另起一套
    assert "video_url(str(i))" in server
    assert "args=(self.state, text, quality, outdir, fps" in server
