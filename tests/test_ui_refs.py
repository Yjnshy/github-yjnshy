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
    """「从视频提取」支持批量多选；封面默认第二秒，不再强制弹挑封面。"""
    # 多选文件对话框 + 批量提取接口
    assert "function pickMediaFiles(" in html
    assert "multiple: 1" in html
    assert "await api('/api/library/extract', { paths })" in html
    # 封面默认第二秒，文案同步
    assert "封面统一用第二秒" in html
    assert "提取完成，默认用的是视频第二秒。" in html
    # 挑封面仍是手动入口（换封面按钮），函数保留
    assert "function openCoverPicker(id, afterExtract)" in html
    # 不再强制弹挑封面
    assert "openCoverPicker(data.track.id, true)" not in html


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
    # 回收按 (slot.z - camZ - 1) / span 的跨度倍数搬，而不是只挪一档
    assert "const span = LAYERS * D;" in module
    assert "const spans = Math.ceil((slot.z - camZ - 1) / span);" in module
    assert "slot.z -= spans * span;" in module
    # 主封面让路只作用到紧邻几层（heroFrontZ + 3 * D），避免远处的层全被藏掉
    assert "slotZ < heroFrontZ + 3 * D" in module
    # 滚轮 delta 夹幅，防止一帧跳几十单位
    assert "Math.max(-120, Math.min(120, event.deltaY))" in module


def test_tunnel_keeps_covers_when_zooming_back(html):
    """往回缩（相机后退）时相机不能退到整条隧道后面，否则所有封面越过雾深
    被染成深色、满屏黑块。修复是按「层」把最远的一层循环搬到最近层前方，
    层距恒为 D，最近封面始终停在相机前 CAM_Z0 左右；stats 里要有 near
    （雾深内可见盒子数）供回归断言。"""
    module = (HTML_PATH.parent / "music-tunnel.js").read_text(encoding="utf-8")
    # 前进按层回收、回缩按层循环补齐，两者都保持同层 slot.z 相等
    assert "while (guard-- > 0)" in module
    assert "if (camZ - maxZ <= CAM_Z0 + D) break;" in module
    assert "const target = maxZ + D;" in module
    assert "if (Math.abs(slot.z - minZ) > 0.001) continue;" in module
    assert "if (slot.flying) continue;            // 正在飞入的只跟着搬位置" in module
    # 不能用「每个盒子各自 floor 取整档」——会层序错乱并出现贴脸巨幅封面
    assert "Math.floor((camZ - slot.z) / span)" not in module
    # 诊断指标：相机前方、雾深之内的盒子数，回缩时不应掉到 0
    assert "near: slots.filter" in module
    assert "d > 2 && d < D * 10.5" in module


def test_version_only_shown_in_titlebar(html):
    """左下角版本号去掉（右上角标题栏已有一份）：元素和 JS 引用要一起删，
    否则 render() 第一次轮询就抛异常、界面永久停更。"""
    assert 'id="version"' not in html
    assert "$('version')" not in html
    assert "class=\"side-meta\"" not in html
    # 右上角标题栏版本号与「关于」弹层仍在
    assert 'id="title-version"' in html
    assert "titleVer.textContent = 'v' + snapshot.version" in html
    assert "const ver = (snapshot && snapshot.version) || '';" in html


def test_recent_block_collapsed_by_default(html):
    """最近添加默认收起，点快捷入口里的「最近添加」才展开、在里面看，再点收起。
    展开后铺成自适应网格，不再是一条带细横向滚动条的窄条。"""
    assert 'id="music-recent-block" hidden' in html
    assert "function toggleRecentBlock()" in html
    assert "act: () => toggleRecentBlock()" in html
    assert 'onclick="toggleRecentBlock()"' in html
    assert "card.dataset.key = d.key;" in html
    assert "card.classList.toggle('on', show)" in html
    # 展开状态在 renderDiscover 重建快捷卡后仍要同步
    assert "d.key === 'recent' && recentBlock && !recentBlock.hidden" in html
    # 横向滚动条换成自适应网格
    assert "grid-template-columns: repeat(auto-fill, minmax(104px, 1fr))" in html
    assert "grid-auto-flow: column" not in html
    assert "overflow-x: auto" not in html


def test_music_grid_uses_whole_page_scrollbar(html):
    """歌单网格视图由 .main 整页滚动（滚动条贴页面右缘贯穿全高），
    隧道视图仍撑满固定高度给 canvas；滚动条要明显、带圆角滑块。"""
    assert ".main::-webkit-scrollbar { width: 12px; }" in html
    assert "border: 3px solid transparent; border-radius: 999px;" in html
    assert "body:not(.music-tunnel-view) #panel-music" in html
    assert "body:not(.music-tunnel-view) #panel-music .music-grid" in html
    assert "flex: 0 0 auto; min-height: auto; overflow: visible;" in html
    # 视图切换要挂/摘 class；WebGL 起不来兜底回网格时也要摘掉
    assert "document.body.classList.toggle('music-tunnel-view', musicView === 'tunnel')" in html
    assert "document.body.classList.remove('music-tunnel-view')" in html


def test_player_icon_buttons_not_white_on_dark_bar(html):
    """播放条里的图标钮不能沿用 --surface-2（浅色主题下是纯白），
    否则铺在 Spotify 深色播放条 / 隧道玻璃条上就是一排刺眼的白圆钮。"""
    # 播放条内统一透明底，不再吃 .icon-btn 的 --surface-2
    assert ".player .icon-btn { background: transparent; }" in html
    # 网格视图的 Spotify 深色条：透明底 + 深灰边框 + 浅灰文字
    assert "body.spotify-skin #panel-music .player:not(.floating) .icon-btn {" in html
    assert "background: transparent; border-color: #3a3a3a; color: #b3b3b3;" in html
    # 隧道浮动玻璃条：半透明白，不是纯白
    assert "background: rgba(255, 255, 255, .06);" in html
    assert "border-color: rgba(255, 255, 255, .16);" in html


def test_speed_cycle_includes_09x(html):
    """倍速循环要包含 0.9×，且显示成「0.9×」而不是「0.90×」。"""
    assert "const SPEEDS = [1, 1.25, 1.5, 2, 0.9, 0.75];" in html
    assert "Number.isInteger(player.speed)" in html
    assert "String(+player.speed.toFixed(2))" in html


def test_tunnel_outer_shell_is_dense(html):
    """隧道外围原来还会再随机删掉 62% 的格子，导致两侧和远端到处是黑洞；
    阈值降到 0.28，把外面一层盒子基本铺满。"""
    module = (HTML_PATH.parent / "music-tunnel.js").read_text(encoding="utf-8")
    assert "h < 0.28" in module
    assert "h < 0.62" not in module
    assert "// 外围（中心 3×3 之外）原来还会再随机删掉 62%" in module


def test_tunnel_matches_grid_margins(html):
    """隧道视图跟网格视图保持同样的页边距和宽度：画面在卡片内铺满（padding 归零），
    卡片圆角裁剪画面，且不再用负边距突破 .main 留白。"""
    assert "body.music-tunnel-view .main { overflow: hidden; }" in html
    assert "padding: 0; border: 0; border-radius: 14px; overflow: hidden;" in html
    assert "body.music-tunnel-view #panel-music .music-tunnel { border-radius: 0; }" in html
    # 不能再靠负边距把卡片撑到窗口边缘
    assert "margin: 0 -30px -16px" not in html
    assert "margin: 0 -16px -12px" not in html
    # 主区左右各 30；底部留白用歌单页末尾 30px 占位块
    # （flex 滚动容器的 padding-bottom / 最后子元素 margin 在 Chrome 下不计入滚动范围）
    assert "padding: 26px 30px 0;" in html
    assert '#panel-music::after { content: ""; display: block; flex: none; height: 30px; }' in html


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



def test_fav_button_hover_not_turned_white(html):
    """收藏红心悬停不能被通用 button:hover 的浅色背景盖成白圆、白心看不见。"""
    assert "button.fav-btn:hover:not(:disabled) {" in html
    assert "background: rgba(18,16,26,.82); border-color: transparent;" in html
    # 图标不拦截指针，hover/click 落在按钮本身
    assert ".fav-btn svg { width: 14px; height: 14px; pointer-events: none; }" in html


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
