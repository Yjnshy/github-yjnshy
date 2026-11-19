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
