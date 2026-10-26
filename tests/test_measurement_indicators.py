"""核对当前小室指示随两组实际状态切换，停止和失联不残留亮灯。"""

import pytest

from cqu_monitor.config import default_settings
from cqu_monitor.ui import MainWindow


def lit_cells(window):
    return {cell for cell, lamp in window.cell_indicators.items() if lamp.property("active")}


def output(cell, **changes):
    return dict(cell=cell, active=True, status="valid", stage="output", phase=2.5, **changes)


def render_states(window, left, right):
    window.render(
        dict(synchronization_source="controller", controllers=dict(left=left, right=right))
    )


def test_idle_has_thirty_unlit_lamps(qtbot):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    assert len(window.cell_indicators) == 30
    assert not lit_cells(window)
    assert window.current_measurement_label.text() == "当前测量：待机"


def test_independent_switch_replaces_lights_and_updates_both_pages(qtbot):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    window.set_running(True)
    render_states(window, output(3), output(20))
    assert lit_cells(window) == {3, 20}
    assert "A组 第03小室" in window.current_measurement_label.text()
    assert "B组 第20小室" in window.current_measurement_label.text()
    render_states(
        window, output(4), dict(cell=20, active=False, stage="interval", status="interval")
    )
    assert lit_cells(window) == {4}
    assert "第03小室" not in window.current_measurement_label.text()
    assert "B组 切换间隔" in window.current_measurement_label.text()
    window.tabs.setCurrentIndex(1)
    assert window.chart_measurement_label.text() == window.current_measurement_label.text()


@pytest.mark.parametrize("status", ["stale", "frozen", "read_error"])
def test_bad_state_never_keeps_last_group_lit(qtbot, status):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    window.set_running(True)
    render_states(window, output(5), output(21))
    render_states(window, {**output(5), "status": status}, output(22))
    assert lit_cells(window) == {22}
    assert "A组 状态待确认" in window.current_measurement_label.text()


def test_stop_and_reset_clear_lights_even_if_a_late_snapshot_arrives(qtbot):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    window.set_running(True)
    render_states(window, output(8), output(28))
    window.set_running(False)
    assert not lit_cells(window)
    assert window.current_measurement_label.text() == "当前测量：待机"
    render_states(window, output(8), output(28))
    assert not lit_cells(window)
    window.set_running(True)
    render_states(window, output(2), output(17))
    window.reset_session("hardware")
    assert not lit_cells(window)
    assert "第02小室" not in window.current_measurement_label.text()


def test_wrong_group_cell_never_lights_another_groups_row(qtbot):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    window.set_running(True)
    render_states(window, output(23), output(7))
    assert not lit_cells(window)


def test_manual_indication_is_explicitly_estimated_and_needs_sync(qtbot):
    settings = default_settings()
    settings["synchronization"] = "manual"
    window = MainWindow(settings)
    qtbot.addWidget(window)
    window.set_running(True, waiting_sync=True)
    assert not lit_cells(window)
    assert "等待人工同步" in window.current_measurement_label.text()
    window.render(dict(synchronized=True, cell_pair=[4, 19], phase=2.5, slot=3))
    assert lit_cells(window) == {4, 19}
    assert "按时间推算" in window.current_measurement_label.text()
    window.set_running(False)
    assert not lit_cells(window)
