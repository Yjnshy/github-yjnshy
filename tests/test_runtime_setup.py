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


def test_confirm_install_false_when_nothing_missing():
    """缺列表为空时不弹询问框，直接返回 False。"""
    assert runtime_setup.confirm_install([]) is False


def test_frozen_fallback_calls_runtime_setup():
    """打包版启动失败要先尝试自动装运行时，装不了才退浏览器模式。"""
    init = (Path(__file__).resolve().parents[1]
            / "src" / "dly" / "gui" / "__init__.py").read_text(encoding="utf-8")
    assert "_try_install_runtimes()" in init
    assert "runtime_setup.missing_components()" in init
    assert "runtime_setup.confirm_install(missing)" in init
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
