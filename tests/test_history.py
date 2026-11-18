"""任务记录：下载 / 录制结果写历史，供界面表格展示。"""

from __future__ import annotations


import pytest

from dly.gui.server import AppState


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """历史现在会落盘，测试必须隔离到临时目录。"""
    monkeypatch.setattr(AppState, "_history_file",
                        staticmethod(lambda: tmp_path / "history.json"))


def test_add_history_keeps_order_and_fields():
    from dly.gui.server import AppState

    state = AppState()
    state.add_history("download", "视频A", "/tmp/a.mp4", 1024, "完成")
    state.add_history("record", "直播B", "/tmp/b.ts", 2048, "已停止")

    rows = state.history
    assert [r["kind"] for r in rows] == ["download", "record"]
    assert rows[0]["name"] == "视频A"
    assert rows[0]["path"] == "/tmp/a.mp4"
    assert rows[0]["size"] == 1024
    assert rows[1]["status"] == "已停止"
    assert rows[0]["id"] < rows[1]["id"]
    assert isinstance(rows[0]["at"], float)


def test_history_is_capped():
    from dly.gui.server import MAX_HISTORY, AppState

    state = AppState()
    for i in range(MAX_HISTORY + 10):
        state.add_history("download", f"f{i}", f"/tmp/{i}", 1)
    assert len(state.history) == MAX_HISTORY
    assert state.history[-1]["name"] == f"f{MAX_HISTORY + 9}"


def test_failed_task_has_no_path():
    from dly.gui.server import AppState

    state = AppState()
    state.add_history("download", "https://x/y", "", 0, "失败")
    assert state.history[-1]["path"] == ""
    assert state.history[-1]["status"] == "失败"


def test_add_history_returns_id_and_can_be_updated():
    """录制/下载开始时先插「进行中」，结束再补路径与状态。"""
    from dly.gui.server import AppState

    state = AppState()
    entry = state.add_history("record", "某场直播", "", 0, "录制中")
    assert entry == state.history[-1]["id"]

    state.update_history(entry, size=1024)
    assert state.history[-1]["size"] == 1024

    state.update_history(entry, path="/tmp/x.ts", size=2048, status="完成")
    row = state.history[-1]
    assert row["path"] == "/tmp/x.ts"
    assert row["size"] == 2048
    assert row["status"] == "完成"
    assert len(state.history) == 1          # 更新不会新增行


def test_update_history_ignores_unknown_id():
    from dly.gui.server import AppState

    state = AppState()
    state.add_history("record", "a", "", 0, "录制中")
    state.update_history(9999, status="完成")     # 不应抛异常
    state.update_history(0, status="完成")
    assert state.history[-1]["status"] == "录制中"


def test_history_survives_app_restart():
    """关掉程序再打开，历史下载/录制还在。"""
    first = AppState()
    first.add_history("download", "昨天的视频", "/videos/a.mp4", 4096, "完成")

    second = AppState()
    assert second.history[-1]["name"] == "昨天的视频"
    assert second.history[-1]["path"] == "/videos/a.mp4"


def test_new_results_counter_for_bell_badge():
    state = AppState()
    assert state.new_results == 0

    state.add_history("download", "进行中", "", 0, "下载中")
    assert state.new_results == 0

    entry = state.history[-1]["id"]
    state.update_history(entry, status="完成", path="/tmp/a.mp4", size=100)
    assert state.new_results == 1

    state.add_history("record", "直播", "/tmp/b.ts", 100, "已停止")
    assert state.new_results == 2

    state.new_results = 0
    assert state.new_results == 0
