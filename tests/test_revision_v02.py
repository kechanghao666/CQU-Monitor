"""用户截图修订：正式采集界面、动态进度、曲线查找与放大。"""

from PySide6.QtCore import Qt, QPointF
from PySide6.QtWidgets import QLabel, QComboBox
import pytest

from cqu_monitor.config import default_settings, load_settings, save_settings, validate_settings
from cqu_monitor.ui import MainWindow


def test_formal_defaults_reject_synthetic_mode():
    settings = default_settings()
    assert settings["mode"] == "hardware"
    assert "simulation_speed" not in settings
    with pytest.raises(ValueError):
        validate_settings({**settings, "mode": "simulation"})


def test_old_configuration_keeps_ports_and_limits_but_removes_demo_mode(tmp_path):
    import json

    settings = default_settings()
    settings.update(mode="simulation", simulation_speed=60, voltage_low=1.5)
    settings["ports"]["current"]["port"] = "COM9"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    loaded = load_settings(path)
    assert loaded["mode"] == "hardware" and "simulation_speed" not in loaded
    assert loaded["ports"]["current"]["port"] == "COM9" and loaded["voltage_low"] == 1.5
    save_settings(path, loaded)
    assert "simulation" not in path.read_text(encoding="utf-8")


@pytest.fixture
def window(qtbot):
    result = MainWindow(default_settings())
    qtbot.addWidget(result)
    result.show()
    qtbot.wait(10)
    return result


def test_title_is_centered_and_removed_text_is_absent(window):
    title = window.findChild(QLabel, "title")
    assert title.alignment() & Qt.AlignmentFlag.AlignHCenter
    midpoint = title.mapTo(window, title.rect().center()).x()
    assert abs(midpoint - window.width() / 2) < 3
    labels = "\n".join(label.text() for label in window.findChildren(QLabel))
    for removed in ("30 小室同步扫描", "电压 · 电流在线监测", "总压为各室最近有效数之和", "模拟"):
        assert removed not in labels
    assert not hasattr(window, "completeness_label")


def test_progress_cycles_as_x_out_of_thirty(window):
    for slot, round_number, progress in ((0, 1, 2), (11, 1, 24), (14, 1, 30), (0, 2, 2)):
        window.render(
            dict(
                synchronized=True,
                slot=slot,
                round=round_number,
                phase=2.5,
                cell_pair=[slot + 1, slot + 16],
                cells=[],
                metrics={},
            )
        )
        assert f"{progress}/30" in window.scan_label.text()
    window.render(dict(synchronized=False))
    assert "0/30" in window.scan_label.text()


def test_search_and_group_filter_without_losing_live_data(window, qtbot):
    window.tabs.setCurrentIndex(1)
    window.chart_search.setText("16")
    assert not window.cell_charts[16].isHidden()
    assert window.cell_charts[1].isHidden() and window.summary_charts["current"].isHidden()
    window.chart_search.setText("电流")
    assert not window.summary_charts["current"].isHidden()
    assert all(chart.isHidden() for chart in window.cell_charts.values())
    window.chart_search.clear()
    window.chart_group.setCurrentIndex(window.chart_group.findData("right"))
    assert not window.cell_charts[30].isHidden() and window.cell_charts[1].isHidden()
    window.add_sample(
        dict(role="voltage_left", cell=1, elapsed=3, value=1.9, mean_value=1.8, valid=True)
    )
    window.chart_group.setCurrentIndex(window.chart_group.findData("all"))
    assert list(window.cell_charts[1].latest_values) == [1.9]
    assert not window.cell_charts[1].isHidden()
    window.chart_search.setText("1-5")
    assert sum(not c.isHidden() for c in window.cell_charts.values()) == 5
    window.chart_search.setText("无匹配内容")
    assert window.chart_count.text() == "显示 0/33 张"


def test_single_click_opens_live_enlargement_and_reset(qtbot, window):
    window.tabs.setCurrentIndex(1)
    chart = window.summary_charts["current"]
    qtbot.mouseClick(
        chart.plot.viewport(), Qt.MouseButton.LeftButton, pos=chart.plot.viewport().rect().center()
    )
    qtbot.waitUntil(lambda: chart.detail_dialog is not None)
    dialog = chart.detail_dialog
    assert dialog.isVisible()
    qtbot.addWidget(dialog)
    window.add_sample(dict(role="current", elapsed=1, value=100, mean_value=100, valid=True))
    assert list(dialog.latest_curve.getData()[1]) == [100]
    window.add_sample(dict(role="current", elapsed=2, value=120, mean_value=110, valid=True))
    assert list(dialog.latest_curve.getData()[1]) == [100, 120]
    assert list(dialog.mean_curve.getData()[1]) == [100, 110]
    dialog.reset_view()
    chart.plot.scene().sigMouseClicked.emit(Click())
    assert chart.detail_dialog is dialog
    window.reset_session("hardware")
    assert dialog.latest_curve.getData()[0] is None
    dialog.close()
    qtbot.waitUntil(lambda: chart.detail_dialog is None)


def test_formal_history_does_not_mix_old_test_sessions(qtbot):
    from cqu_monitor.dialogs import HistoryDialog

    class Store:
        def list_sessions(self):
            return [dict(id="old", mode="simulation", started_at="2026-10-16", status="completed")]

    dialog = HistoryDialog(Store())
    qtbot.addWidget(dialog)
    assert dialog.session_table.rowCount() == 0


class Click:
    def button(self):
        return Qt.MouseButton.LeftButton

    def scenePos(self):
        return QPointF(100, 100)


def test_settings_has_only_real_connections(qtbot):
    from cqu_monitor.dialogs import SettingsDialog

    dialog = SettingsDialog(default_settings())
    qtbot.addWidget(dialog)
    assert not hasattr(dialog, "mode_combo") and not hasattr(dialog, "speed_combo")
    assert all(
        "模拟" not in dialog.findChildren(QComboBox)[i].currentText()
        for i in range(len(dialog.findChildren(QComboBox)))
    )
