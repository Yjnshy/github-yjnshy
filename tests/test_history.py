"""任务记录：下载 / 录制结果写历史，供界面表格展示。"""

from __future__ import annotations


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
