"""窗口几何计算测试。

高分屏上最烦人的问题就是「窗口比屏幕高，底部按钮被任务栏挡住」，
所以这里把「窗口必须完整落在可用工作区内」当成硬约束来测。
"""

from __future__ import annotations

import pytest

from media_toolkit.gui import desktop


@pytest.fixture
def fake_screen(monkeypatch):
    def apply(area, scale=1.0):
        monkeypatch.setattr(desktop, "_work_area", lambda: area)
        monkeypatch.setattr(desktop, "_dpi_scale", lambda: scale)
    return apply


def test_window_fits_1080p_screen(fake_screen):
    fake_screen((0, 0, 1920, 1040))          # 1080p 去掉任务栏
    (w, h), (x, y) = desktop._window_geometry()
    assert w <= 1920 and h <= 1040
    assert x >= 0 and y >= 0
    assert x + w <= 1920
    assert y + h <= 1040


def test_window_fits_when_scaled_150_percent(fake_screen):
    """2560x1440 屏、150% 缩放：可用区域换算成逻辑像素后仍要装得下。"""
    fake_screen((0, 0, 2560, 1400), scale=1.5)
    (w, h), (x, y) = desktop._window_geometry()
    assert w <= int(2560 / 1.5)
    assert h <= int(1400 / 1.5)
    assert y + h <= int(1400 / 1.5)


def test_window_is_centered(fake_screen):
    fake_screen((0, 0, 1600, 900))
    (w, h), (x, y) = desktop._window_geometry()
    assert abs((x + w / 2) - 800) <= 2
    assert abs((y + h / 2) - 450) <= 2


def test_small_screen_still_usable(fake_screen):
    """1366x768 的老笔记本：窗口不能大到溢出，也不能小到没法用。"""
    fake_screen((0, 0, 1366, 728))
    (w, h), _ = desktop._window_geometry()
    assert w <= 1366 and h <= 728
    assert w >= 640 and h >= 480


def test_unknown_work_area_falls_back(fake_screen):
    """取不到工作区时退回固定尺寸，不能抛异常。"""
    fake_screen((0, 0, 0, 0))
    size, pos = desktop._window_geometry()
    assert size == desktop.WINDOW_SIZE
    assert pos == (None, None)


def test_min_size_never_exceeds_window(fake_screen):
    fake_screen((0, 0, 800, 600))
    (w, h), _ = desktop._window_geometry()
    assert desktop.MIN_SIZE[0] >= w or w >= 640
    assert min(desktop.MIN_SIZE[0], w) <= w
    assert min(desktop.MIN_SIZE[1], h) <= h
