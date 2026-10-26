"""界面契约验证：不访问真实串口，也不启动采集线程。"""

import pytest
from PySide6.QtCore import Qt

from cqu_monitor.dialogs import HistoryDialog, SettingsDialog
from cqu_monitor.ui import MainWindow


@pytest.fixture
def settings():
    return {
        "mode": "hardware",
        "step_seconds": 6,
        "sample_interval": 1,
        "valid_start": 2,
        "valid_end": 4,
        "plot_points": 4,
        "voltage_low": None,
        "voltage_high": None,
        "current_low": None,
        "current_high": None,
        "ports": {
            role: {"port": "", "baudrate": 9600, "unit": 1, "timeout": 0.1}
            for role in ("voltage_left", "voltage_right", "current")
        },
    }


@pytest.fixture
def window(qtbot, settings):
    widget = MainWindow(settings)
    qtbot.addWidget(widget)
    return widget


def test_title_two_pages_and_thirty_rows(window):
    assert window.windowTitle() == "重庆大学电解水制氢系统"
    assert window.tabs.count() == 2
    assert window.left_table.rowCount() == window.right_table.rowCount() == 15
    assert window.left_table.item(0, 0).text() == "01"
    assert window.right_table.item(14, 0).text() == "30"
    assert all(label.text() == "—" for label in window.metric_values.values())
    assert not hasattr(window, "mode_badge")


def test_last_cell_remains_accessible_at_default_size(qtbot, window):
    window.show()
    qtbot.wait(50)
    for table in (window.left_table, window.right_table):
        rectangle = table.visualItemRect(table.item(14, 0))
        assert (
            rectangle.bottom() < table.viewport().height() or table.verticalScrollBar().isVisible()
        )


@pytest.mark.parametrize(
    "button,signal",
    [
        ("start_button", "start_requested"),
        ("stop_button", "stop_requested"),
        ("sync_button", "sync_requested"),
        ("settings_button", "settings_requested"),
        ("export_button", "export_requested"),
        ("history_button", "history_requested"),
    ],
)
def test_action_signal(qtbot, window, button, signal):
    window.set_running(button not in ("start_button", "settings_button"))
    with qtbot.waitSignal(getattr(window, signal), timeout=1000):
        qtbot.mouseClick(getattr(window, button), Qt.MouseButton.LeftButton)


def test_close_emits_before_accepting(qtbot, window):
    with qtbot.waitSignal(window.close_requested, timeout=1000):
        window.close()


def test_snapshot_values_missing_values_and_active_pair(window):
    window.render(
        {
            "mode": "hardware",
            "elapsed": 62.3,
            "synchronized": True,
            "round": 1,
            "phase": 2.3,
            "cell_pair": [11, 26],
            "cells": [
                {"cell": 1, "latest": 0, "mean": 0, "age": 4, "count": 2, "status": "valid"},
                {
                    "cell": 2,
                    "latest": None,
                    "mean": 1.5,
                    "age": None,
                    "count": 0,
                    "status": "missing",
                },
            ],
            "metrics": {
                "current": None,
                "total_voltage": None,
                "avg_total_voltage": 51.25,
                "power_kw": None,
                "avg_power_kw": 0.15,
                "complete": False,
                "reason": "incomplete",
            },
            "alarms": [{"key": "test", "level": "warning", "message": "电压偏高"}],
        }
    )
    assert window.left_table.item(0, 1).text() == "0.000"
    assert window.left_table.item(1, 1).text() == "—"
    assert window.left_table.item(1, 2).text() == "1.500"
    assert "4.0" in window.left_table.item(0, 1).toolTip()
    assert window.left_table.item(10, 0).background().color().name() == "#dcf4f0"
    assert window.metric_values["current"].text() == "—"
    assert window.metric_values["avg_total_voltage"].text() == "51.250"
    assert not hasattr(window, "alarm_label")


def test_valid_samples_only_and_bounded_mean_curves(window):
    for index in range(10):
        window.add_sample(
            {
                "role": "voltage_left",
                "cell": 1,
                "elapsed": index,
                "value": index,
                "mean_value": index / 2,
                "valid": True,
            }
        )
    window.add_sample(
        {
            "role": "voltage_left",
            "cell": 1,
            "elapsed": 11,
            "value": 900,
            "mean_value": 99,
            "valid": False,
        }
    )
    assert list(window.cell_charts[1].latest_values) == [6, 7, 8, 9]
    assert list(window.cell_charts[1].mean_values) == [3, 3.5, 4, 4.5]
    assert len(window.cell_charts) == 30
    window.reset_session("hardware")
    assert len(window.cell_charts[1].latest_values) == 0
    assert window.metric_values["current"].text() == "—"


def test_summary_invalid_points_are_gaps(window):
    window.add_summary(
        {
            "elapsed": 1,
            "total_voltage": None,
            "current": 3,
            "power_kw": None,
            "avg_total_voltage": None,
            "avg_power_kw": None,
        }
    )
    assert len(window.summary_charts["total_voltage"].times) == 0
    assert list(window.summary_charts["current"].latest_values) == [3]


def test_current_samples_include_cumulative_mean_without_summary_duplicates(window):
    window.add_sample(
        {
            "role": "current",
            "cell": None,
            "elapsed": 1,
            "value": 12,
            "mean_value": 11.5,
            "valid": True,
        }
    )
    window.add_summary({"elapsed": 1, "current": 12})
    window.add_sample(
        {"role": "current", "elapsed": 2, "value": None, "mean_value": 11.5, "valid": False}
    )
    chart = window.summary_charts["current"]
    import math

    assert chart.latest_values[0] == 12
    assert math.isnan(chart.latest_values[1])
    assert list(chart.mean_values) == [11.5, 11.5]


def test_stale_cell_keeps_last_reading_with_quality_warning(window):
    window.render(
        {
            "cells": [
                {"cell": 1, "latest": 1.8, "mean": 1.7, "count": 2, "age": 101, "status": "stale"}
            ]
        }
    )
    assert window.left_table.item(0, 1).text() == "1.800"
    assert window.left_table.item(0, 2).text() == "1.700"
    assert "101.0" in window.left_table.item(0, 1).toolTip()


def test_events_are_bounded(window):
    for index in range(350):
        window.append_event("2026-10-16T01:00:00+08:00", "info", str(index))
    assert window.event_log.document().blockCount() <= 301


def test_settings_zero_threshold_and_preserves_rules(qtbot, settings):
    settings["voltage_low"] = 0
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)
    assert dialog.threshold_enabled["voltage_low"].isChecked()
    values = dialog.values()
    assert values["voltage_low"] == 0
    assert values["step_seconds"] == 6 and values["valid_start"] == 2
    assert values["valid_end"] == 4 and values["sample_interval"] == 1
    assert settings["ports"]["current"]["port"] == ""


def test_settings_rejects_missing_duplicate_ports_and_inverted_limits(qtbot, settings):
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)
    for controls in dialog.port_controls.values():
        controls["port"].setCurrentText("")
    dialog.accept()
    assert dialog.result() == 0 and "串口" in dialog.error_label.text()
    for controls in dialog.port_controls.values():
        controls["port"].setCurrentText("COM9")
    dialog.accept()
    assert dialog.result() == 0 and "相同" in dialog.error_label.text()
    for index, controls in enumerate(dialog.port_controls.values(), 1):
        controls["port"].setCurrentText(f"COM{index}")
    dialog.threshold_enabled["voltage_low"].setChecked(True)
    dialog.threshold_enabled["voltage_high"].setChecked(True)
    dialog.threshold_inputs["voltage_low"].setValue(10)
    dialog.threshold_inputs["voltage_high"].setValue(1)
    dialog.accept()
    assert dialog.result() == 0 and "下限" in dialog.error_label.text()
    assert dialog.values()["mode"] == "hardware"


class MemoryStore:
    def __init__(self):
        self.queries = []
        self.exports = []

    def list_sessions(self):
        return [
            {
                "id": "session-1",
                "mode": "hardware",
                "started_at": "2026-10-16T00:00:00+08:00",
                "ended_at": None,
                "status": "running",
            }
        ]

    def get_session(self, session_id):
        return {"id": session_id, "settings": {}, "snapshot": {}}

    def query_samples(self, session_id, **kwargs):
        self.queries.append((session_id, kwargs))
        return (
            [
                {
                    "wall_time": "2026-10-16T00:01:00+08:00",
                    "elapsed": i,
                    "role": "current",
                    "cell": None,
                    "value": i,
                    "mean_value": i,
                    "valid": True,
                    "status": "valid",
                }
                for i in range(101)
            ]
            if kwargs["offset"] == 0
            else []
        )

    def export_session(self, session_id, destination, **kwargs):
        self.exports.append((session_id, destination, kwargs))


def test_history_filters_pagination_and_background_export(qtbot, tmp_path):
    store = MemoryStore()
    dialog = HistoryDialog(store)
    qtbot.addWidget(dialog)
    assert dialog.sample_table.rowCount() == 100
    assert dialog.next_button.isEnabled()
    dialog.cell_combo.setCurrentIndex(1)
    dialog.valid_only.setChecked(True)
    dialog.query()
    assert store.queries[-1][1]["cell"] == 0
    assert store.queries[-1][1]["valid_only"] is True
    dialog.next_page()
    assert store.queries[-1][1]["offset"] == 100
    dialog.start_input.setText("not-a-date")
    dialog.query()
    assert "ISO" in dialog.status_label.text()
    dialog.start_input.clear()
    dialog.start_export(str(tmp_path / "history.xlsx"))
    qtbot.waitUntil(lambda: dialog.export_thread is None, timeout=5000)
    assert len(store.exports) == 1
    assert "完成" in dialog.status_label.text()


def test_summary_fault_breaks_line_and_recovers(window):
    import math

    chart = window.summary_charts["power_kw"]
    window.add_summary({"elapsed": 1, "power_kw": 2, "avg_power_kw": 2})
    window.add_summary({"elapsed": 2, "power_kw": None, "avg_power_kw": 2})
    window.add_summary({"elapsed": 3, "power_kw": 3, "avg_power_kw": 2.5})
    assert list(chart.times) == [1, 2, 3]
    assert math.isnan(chart.latest_values[1])
    assert chart.latest_values[-1] == 3


def test_main_close_closes_enlarged_chart(qtbot, window):
    window.show()
    chart = window.cell_charts[1]
    chart.open_detail()
    detail = chart.detail_dialog
    assert detail.isVisible()
    window.close()
    assert not detail.isVisible()


def test_compact_window_keeps_controls_inside_client_area(qtbot, window):
    window.resize(1200, 700)
    window.show()
    qtbot.wait(50)
    assert window.height() <= 720
    assert (
        window.event_log.mapTo(window, window.event_log.rect().bottomRight()).y() < window.height()
    )
    assert all(label.isVisible() for label in window.metric_values.values())
    for table in (window.left_table, window.right_table):
        assert (
            table.visualItemRect(table.item(14, 0)).bottom() < table.viewport().height()
            or table.verticalScrollBar().isVisible()
        )
