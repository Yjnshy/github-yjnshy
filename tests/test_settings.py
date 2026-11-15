"""设置页持久化与 /api/settings 接口的回归测试。

覆盖：局部写回 config.yaml 时保留未涉及的键、非法值规整、房间列表过滤、
界面偏好读回、以及 HTTP 接口与静态资源路由。写文件一律落在 tmp_path，
绝不碰用户真实的 config.yaml。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from dly.core import config as cfgmod
from dly.core.config import load_config, save_settings, settings_from_config
from dly.gui import server as srv

ENV_KEYS = ("DLY_OUTPUT_DIR", "DLY_QUALITY", "DLY_FPS", "DLY_PROXY",
            "DLY_COOKIE_BROWSER", "DLY_COOKIES_FILE")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _seed(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        'output_dir: "./downloads"\n'
        'proxy: "http://127.0.0.1:7890"\n'
        'ffmpeg_path: "D:/tools/ffmpeg.exe"\n'
        'cookies:\n'
        '  browser: "firefox"\n'
        '  cookies_file: "D:/cookies.txt"\n'
        '  cache_days: 3\n'
        'record:\n'
        '  format: "mkv"\n'
        '  max_retries: 9\n'
        '  rooms:\n'
        '    - "https://live.douyin.com/1"\n',
        encoding="utf-8")
    return path


def test_default_ui_preferences():
    settings = settings_from_config(cfgmod.AppConfig())
    assert settings["theme"] == "system"
    assert settings["sound"] is True
    assert settings["log_level"] == "info"
    assert settings["rooms"] == []


def test_save_settings_updates_whitelist_and_keeps_other_keys(tmp_path):
    path = _seed(tmp_path)
    save_settings({
        "output_dir": "D:/影片",
        "quality": "720",
        "fps": 30,
        "keep_original": True,
        "browser": "edge",
        "record_format": "mp4",
        "segment_minutes": 15,
        "poll_interval": 120,
        "record_fps": 25,
        "rooms": [" https://live.bilibili.com/6 ", "# 这行忽略", "",
                  "x" * 600],
        "theme": "dark",
        "sound": False,
        "log_level": "warn",
    }, str(path))

    cfg = load_config(str(path))
    settings = settings_from_config(cfg)
    assert settings["output_dir"] == str(Path("D:/影片"))
    assert settings["quality"] == "720"
    assert settings["fps"] == 30
    assert settings["keep_original"] is True
    assert settings["browser"] == "edge"
    assert settings["record_format"] == "mp4"
    assert settings["segment_minutes"] == 15
    assert settings["poll_interval"] == 120
    assert settings["record_fps"] == 25
    assert settings["rooms"] == ["https://live.bilibili.com/6"]
    assert settings["theme"] == "dark"
    assert settings["sound"] is False
    assert settings["log_level"] == "warn"

    # 未涉及的键必须原样保留，且不会写入 Cookie 明文
    assert cfg.proxy == "http://127.0.0.1:7890"
    assert cfg.ffmpeg_path == "D:/tools/ffmpeg.exe"
    assert cfg.cookies.cookies_file == "D:/cookies.txt"
    assert cfg.cookies.cache_days == 3
    assert cfg.record.max_retries == 9
    assert "sessionid" not in path.read_text(encoding="utf-8")


def test_save_settings_partial_update_keeps_current_values(tmp_path):
    path = _seed(tmp_path)
    save_settings({"sound": False}, str(path))
    cfg = load_config(str(path))
    assert cfg.ui.sound is False
    assert cfg.quality == "best"
    assert cfg.cookies.browser == "firefox"
    assert cfg.record.format == "mkv"
    assert cfg.ui.theme == "system"


def test_save_settings_normalizes_invalid_values(tmp_path):
    path = _seed(tmp_path)
    save_settings({
        "quality": "4k", "fps": 999, "record_fps": -5,
        "poll_interval": 1, "segment_minutes": 99999,
        "browser": "safari", "record_format": "exe",
        "theme": "neon", "log_level": "trace",
        "rooms": [None, 123, "   "],
    }, str(path))
    settings = settings_from_config(load_config(str(path)))
    assert settings["quality"] == "best"       # 原文件没写 quality，回退默认
    assert settings["fps"] == 120
    assert settings["record_fps"] == 0
    assert settings["poll_interval"] == 10
    assert settings["segment_minutes"] == 1440
    assert settings["browser"] == "firefox"   # 非法值回退到当前生效值
    assert settings["record_format"] == "mkv"
    assert settings["theme"] == "system"
    assert settings["log_level"] == "info"
    assert settings["rooms"] == []


def test_invalid_choice_falls_back_to_current_value(tmp_path):
    path = _seed(tmp_path)
    save_settings({"browser": "safari", "quality": "4k"}, str(path))
    settings = settings_from_config(load_config(str(path)))
    # _choice 的默认值来自当前配置：原文件里是 firefox，quality 缺省是 best
    assert settings["browser"] == "firefox"
    assert settings["quality"] == "best"


def test_save_settings_creates_file_when_missing(tmp_path):
    path = tmp_path / "nested" / "config.yaml"
    save_settings({"theme": "light", "output_dir": "D:/out"}, str(path))
    assert path.is_file()
    assert settings_from_config(load_config(str(path)))["theme"] == "light"


def test_ui_values_are_read_from_yaml(tmp_path):
    path = _seed(tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('ui:\n  theme: "dark"\n  sound: false\n'
                     '  log_level: "error"\n')
    cfg = load_config(str(path))
    assert (cfg.ui.theme, cfg.ui.sound, cfg.ui.log_level) == (
        "dark", False, "error")


# ------------------------------------------------------------------ HTTP 接口

@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "DEFAULT_PORT", 8913)
    path = _seed(tmp_path)
    monkeypatch.setattr(srv, "load_config", lambda: load_config(str(path)))
    monkeypatch.setattr(srv, "config_file_path", lambda: path)
    monkeypatch.setattr(srv, "save_settings",
                        lambda data: save_settings(data, str(path)))
    ctx = srv.create_app(desktop=True)
    ctx.start()
    try:
        yield ctx, path
    finally:
        ctx.shutdown()


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as res:
        return res.status, res.read().decode("utf-8")


def _post(base: str, path: str, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def test_get_settings(app):
    ctx, path = app
    status, body = _get(ctx.url, "/api/settings")
    data = json.loads(body)
    assert status == 200
    assert data["settings"]["browser"] == "firefox"
    assert data["settings"]["theme"] == "system"
    assert data["path"] == str(path)


def test_post_settings_persists_and_returns_fresh_values(app):
    ctx, path = app
    status, data = _post(ctx.url, "/api/settings",
                         {"quality": "480", "theme": "dark", "sound": False})
    assert status == 200
    assert data["ok"] is True
    assert data["settings"]["quality"] == "480"
    assert data["settings"]["theme"] == "dark"
    assert data["settings"]["sound"] is False
    assert settings_from_config(load_config(str(path)))["quality"] == "480"


def test_state_snapshot_includes_settings(app):
    ctx, _ = app
    status, body = _get(ctx.url, "/api/state")
    assert status == 200
    assert json.loads(body)["snapshot"]["settings"]["browser"] == "firefox"


def test_pixel_toggle_script_is_served(app):
    ctx, _ = app
    status, body = _get(ctx.url, "/pixel-toggle.js")
    assert status == 200
    assert "PixelToggle" in body


# ------------------------------------------------------- 保存目录持久化回归
# 用户反馈：设置页改了默认目录，下载页没跟着变，重启后又回到最初目录。
# 这里覆盖「写入位置 == 读取位置」「完整表单经 HTTP 写入后重启可读回」
# 「主题自动保存这种局部 POST 不会抹掉目录」三条链路。

FULL_FORM = {
    "output_dir": "D:/影片",
    "quality": "best",
    "fps": 0,
    "keep_original": False,
    "browser": "chrome",
    "record_format": "ts",
    "segment_minutes": 0,
    "poll_interval": 60,
    "record_fps": 0,
    "rooms": [],
    "theme": "dark",
    "sound": True,
    "log_level": "info",
}


def test_config_path_is_absolute_and_reload_keeps_output_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cfgmod.paths, "app_dir", lambda: tmp_path)
    target = cfgmod.config_file_path()
    assert target.is_absolute()

    written = cfgmod.save_settings(FULL_FORM)
    assert written == target.resolve()
    # 模拟重启：不带任何参数重新加载，走的是默认搜索顺序
    assert load_config().output_dir == str(Path("D:/影片"))
    assert settings_from_config(load_config())["output_dir"] == str(Path("D:/影片"))

    # 主题开关的自动保存只提交 {theme}，不能把已保存的目录抹掉
    cfgmod.save_settings({"theme": "light"})
    assert load_config().output_dir == str(Path("D:/影片"))
    assert "output_dir" in written.read_text(encoding="utf-8")


def test_post_full_form_persists_output_dir(app):
    ctx, path = app
    status, data = _post(ctx.url, "/api/settings", FULL_FORM)
    assert status == 200
    assert data["ok"] is True
    assert Path(data["path"]).is_absolute()
    assert data["settings"]["output_dir"] == str(Path("D:/影片"))
    # 文件内容与「重新打开文件读取」都要拿到新目录
    assert "output_dir" in path.read_text(encoding="utf-8")
    assert load_config(str(path)).output_dir == str(Path("D:/影片"))


def test_index_has_settings_guard_and_live_sync(app):
    """界面必须能提示未保存，且目录选择结果能触发联动事件。"""
    ctx, _ = app
    _, html = _get(ctx.url, "/")
    for token in ('id="set-state"', 'id="set-save-top"', 'syncOutputDir',
                  "dispatchEvent(new Event('input'", 'settingsDirty'):
        assert token in html, f"缺少 {token}"


def test_sidebar_theme_toggle_replaces_quit_button(app):
    """侧栏底部主题开关占据原退出按钮位置；桌面版关窗即退出，不再需要按钮。"""
    ctx, _ = app
    _, html = _get(ctx.url, "/")
    bottom = html.split('<div class="side-bottom">', 1)[1].split('</aside>', 1)[0]
    assert 'quitApp' not in html
    assert '>退出程序</button>' not in html
    assert '<div class="side-links">' not in html
    assert 'id="theme-toggle-mini"' in bottom
    assert (bottom.index('side-status') < bottom.index('id="theme-toggle-mini"')
            < bottom.index('side-meta'))
    assert '关闭窗口即停止值守并退出程序' in html
    assert '关闭浏览器页面服务仍在运行' in html


def test_settings_cards_are_single_column(app):
    """设置页卡片由两列并排改为单列。"""
    ctx, _ = app
    _, html = _get(ctx.url, "/")
    assert '.settings-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 14px; }' in html
    assert 'repeat(2, minmax(0, 1fr))' not in html
    assert '.settings-grid .row .field { flex: 1 1 160px; }' in html
