"""桌面运行时检测 / 自动安装的回归测试。

下载与提权安装没法在测试里真跑（需要联网 + UAC），所以这里只锁住：
检测逻辑不抛异常、组件清单与安装表对齐、链接是微软官方永久链接、
启动回退逻辑确实接入了自动安装。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dly.gui import runtime_setup


def test_missing_components_returns_known_names():
    """检测结果只能是这两个组件，或缺省为空。"""
    missing = runtime_setup.missing_components()
    assert isinstance(missing, list)
    assert set(missing) <= {"dotnet48", "webview2"}


def test_detection_does_not_raise():
    """查注册表出问题时只能返回 False，不能把启动流程带崩。"""
    assert isinstance(runtime_setup.has_dotnet48(), bool)
    assert isinstance(runtime_setup.has_webview2(), bool)


def test_installers_and_labels_align():
    """每个组件都要有显示名、下载地址、文件名和静默参数。"""
    assert set(runtime_setup.LABELS) == set(runtime_setup.INSTALLERS)
    for name, (url, filename, params) in runtime_setup.INSTALLERS.items():
        assert url.startswith("https://go.microsoft.com/"), name
        assert filename.endswith(".exe"), name
        assert params.startswith("/"), name


def test_install_with_empty_list_is_noop():
    """没缺东西就不该下载 / 弹窗。"""
    assert runtime_setup.install([]) is True


def test_prompt_skips_when_nothing_missing():
    """缺列表为空时不弹询问框。"""
    assert runtime_setup.prompt_runtime_action([]) == "skip"


def test_official_download_pages():
    """手动安装的官方页面要指向微软域名。"""
    assert runtime_setup.DOWNLOAD_PAGES["dotnet48"].startswith("https://dotnet.microsoft.com/")
    assert runtime_setup.DOWNLOAD_PAGES["webview2"].startswith("https://developer.microsoft.com/")
    assert not any("runtime" in url for url in runtime_setup.DOWNLOAD_PAGES.values())


def test_frozen_fallback_calls_runtime_setup():
    """打包版启动失败要先尝试自动装运行时，装不了才退浏览器模式。"""
    init = (Path(__file__).resolve().parents[1]
            / "src" / "dly" / "gui" / "__init__.py").read_text(encoding="utf-8")
    assert "_try_install_runtimes(str(exc))" in init
    assert "runtime_setup.missing_components()" in init
    assert "runtime_setup.prompt_runtime_action(targets, repair=repair)" in init
    assert 'action == "pages"' in init
    assert "runtime_setup.open_download_pages(targets)" in init
    assert "runtime_setup.relaunch()" in init
    assert "launch_web(open_browser=True, idle_exit_seconds=150)" in init


def test_download_failure_returns_false(tmp_path):
    """连不上时只能返回 False，不能抛异常中断安装流程。"""
    assert runtime_setup._download("http://127.0.0.1:1/nope.exe",
                                   tmp_path / "x.exe", timeout=1.5) is False
    assert not (tmp_path / "x.exe").exists()


def test_run_elevated_missing_exe_returns_none(tmp_path):
    """文件不存在时提权启动会失败，必须安全返回 None（不弹 UAC）。"""
    assert runtime_setup._run_elevated(tmp_path / "not_here.exe", "/q") is None


def test_download_failure_returns_false(tmp_path):
    """连不上时只能返回 False，不能抛异常中断安装流程。"""
    assert runtime_setup._download("http://127.0.0.1:1/nope.exe",
                                   tmp_path / "x.exe", timeout=1.5) is False
    assert not (tmp_path / "x.exe").exists()


def test_run_elevated_missing_exe_returns_none(tmp_path):
    """文件不存在时提权启动会失败，必须安全返回 None（不弹 UAC）。"""
    assert runtime_setup._run_elevated(tmp_path / "not_here.exe", "/q") is None


def test_version_matches_pyproject():
    """界面显示的版本来自 dly.__version__，必须和 pyproject 同步。"""
    import re

    import dly

    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert dly.__version__ == declared


def test_installers_are_small_web_downloads():
    """压缩包不携带大安装包：自动安装只下载 1~2MB 的联网引导包。"""
    assert "go.microsoft.com/fwlink" in runtime_setup.WEBVIEW2_URL
    assert "go.microsoft.com/fwlink" in runtime_setup.DOTNET48_URL
    assert not hasattr(runtime_setup, "installer_path")
    assert not hasattr(runtime_setup, "_bundled_installer_dir")


def test_detect_runtime_failure_text():
    """注册表显示已装但 pythonnet 仍失败时，必须给强制修复机会。"""
    import dly.gui.runtime_setup as rs
    assert rs.looks_like_runtime_failure(
        "桌面窗口启动失败：Failed to resolve Python.Runtime.Loader.Initialize")
    assert not rs.looks_like_runtime_failure("磁盘空间不足")
    assert set(rs.repair_targets()) == {"dotnet48", "webview2"}


def test_cookie_file_roundtrip(tmp_path, monkeypatch):
    """cookies.txt 路径要能保存并读回；不存在的文件会被清空。"""
    from dly.core.config import AppConfig, save_settings, settings_from_config
    from dly.core import config as cfgmod

    monkeypatch.setattr(cfgmod, "config_file_path", lambda: tmp_path / "config.yaml")
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    saved = tmp_path / "config.yaml"
    save_settings({"cookies_file": str(cookie_file)})
    assert settings_from_config(cfgmod.load_config(str(saved)))["cookies_file"] == str(cookie_file)
    save_settings({"cookies_file": str(tmp_path / "missing.txt")})
    assert settings_from_config(cfgmod.load_config(str(saved)))["cookies_file"] == ""


def test_cookie_error_explains_app_bound():
    """Edge 的 App-Bound 加密必须给出 cookies.txt 导入指引。"""
    from dly.cookies.manager import _explain_cookie_error
    hint = _explain_cookie_error("Failed to decrypt with DPAPI (app-bound)", "edge")
    assert "App-Bound" in hint and "cookies.txt" in hint and "设置页" in hint


def test_cookie_file_picker_endpoint_exists():
    """设置页要能弹系统文件框选回 cookies.txt。"""
    import pathlib
    server = (pathlib.Path(__file__).resolve().parents[1]
              / "src" / "dly" / "gui" / "server.py").read_text(encoding="utf-8")
    html = (pathlib.Path(__file__).resolve().parents[1]
            / "src" / "dly" / "gui" / "assets" / "index.html").read_text(encoding="utf-8")
    assert 'path == "/api/pick-cookie-file"' in server
    assert 'from dly.core.folder_dialog import pick_file' in server
    assert 'id="set-cookies-file"' in html
    assert "chooseCookieFile()" in html


def test_manual_cookie_roundtrip(tmp_path):
    """设置页粘贴的 Cookie 要能保存并作为请求头使用。"""
    from dly.core import config as cfgmod
    from dly.core.config import save_settings, settings_from_config
    from dly.cookies.manager import CookieManager

    saved = tmp_path / "config.yaml"
    header = "sessionid=abc123; odin_tt=xyz"
    save_settings({"manual_cookie": header}, str(saved))
    cfg = cfgmod.load_config(str(saved))
    assert cfg.cookies.manual_cookie == header
    assert settings_from_config(cfg)["manual_cookie"] == header
    manager = CookieManager(cfg.cookies, use_cache=False)
    assert manager._from_manual("douyin") == header


def test_manual_cookie_ui_is_wired():
    """设置页要有手动 Cookie 输入框，并纳入未保存检测。"""
    html = (Path(__file__).resolve().parents[1]
            / "src" / "dly" / "gui" / "assets" / "index.html").read_text(encoding="utf-8")
    assert 'id="set-manual-cookie"' in html
    assert "manual_cookie: $('set-manual-cookie').value.trim()" in html
    assert "'set-manual-cookie'" in html


def test_open_interface_uses_browser_app_window(monkeypatch):
    """浏览器模式用真实安装路径开应用窗口，确保双击后有可见界面。"""
    from dly.gui import server

    calls = []
    monkeypatch.setattr(server, "browser_exe",
                        lambda name: "C:/fake/msedge.exe" if name == "msedge.exe" else "")
    monkeypatch.setattr(server.subprocess, "Popen",
                        lambda args, **kw: calls.append(args))
    assert server.open_interface("http://127.0.0.1:8765/") is True
    assert calls and calls[0][0] == "C:/fake/msedge.exe"
    assert "--app=http://127.0.0.1:8765/" in calls[0]


def test_existing_instance_detected(tmp_path):
    """重复双击时扫描已有服务，返回它的 URL。"""
    from dly.gui import server

    assert callable(server._existing_instance_url)
    url = server._existing_instance_url()
    assert url == "" or url.startswith("http://127.0.0.1:8")
