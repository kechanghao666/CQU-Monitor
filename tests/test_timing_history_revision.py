"""可调采样窗口及历史清空的行为回归；只使用临时数据库。"""

import csv
import json
import sqlite3

import pytest
from PySide6.QtWidgets import QMessageBox

from cqu_monitor.config import default_settings, load_settings, save_settings, validate_settings
from cqu_monitor.controller import Controller
from cqu_monitor.dialogs import HistoryDialog, SettingsDialog
from cqu_monitor.engine import Engine
from cqu_monitor.pulse import PulseTracker
from cqu_monitor.storage import SessionStore, StorageError
from cqu_monitor.ui import MainWindow

WALL = "2026-10-16T09:00:00+08:00"


def timing(**changes):
    settings = default_settings()
    settings.update(step_seconds=10, valid_start=3, valid_end=7, **changes)
    return settings


def test_custom_timing_roundtrips_without_changing_defaults(tmp_path):
    path = tmp_path / "settings.json"
    settings = timing()
    save_settings(path, settings)
    assert load_settings(path) == settings
    defaults = default_settings()
    assert (defaults["step_seconds"], defaults["valid_start"], defaults["valid_end"]) == (6, 2, 4)


@pytest.mark.parametrize(
    "values",
    [
        (0, 0, 1),
        (100, 2, 4),
        (6, -1, 3),
        (6, 3, 3),
        (6, 4, 3),
        (6, 2, 7),
        (True, 0, 1),
        (6, 2, float("nan")),
        (6.001, 2, 4),
    ],
)
def test_invalid_timing_is_rejected_at_configuration_and_engine(values):
    settings = default_settings()
    settings.update(zip(("step_seconds", "valid_start", "valid_end"), values))
    with pytest.raises(ValueError):
        validate_settings(settings)
    with pytest.raises(ValueError):
        Engine(settings, "bad")


def test_manual_sampling_moves_cells_using_configured_width():
    settings = timing(synchronization="manual")
    engine = Engine(settings, "custom")
    engine.start(0, WALL)
    engine.synchronize(0)
    assert not engine.accept("voltage_left", 2, 2.1, 2.2, WALL)["valid"]
    sample = engine.accept("voltage_left", 2, 13.1, 13.2, WALL)
    assert sample["valid"] and sample["cell"] == 2
    assert not engine.accept("voltage_left", 2, 16.9, 17, WALL)["valid"]
    assert engine.accept("current", 100, 18, 18.1, WALL)["valid"]
    assert engine.snapshot(18.2)["metrics"]["power_kw"] == pytest.approx(0.2)


def packet(t, width=1000):
    return dict(
        group="left",
        started=t,
        finished=t + 0.02,
        parameters=[width, 0, 0, 15],
        registers=[1, round((10 - t) * 100), 0, 0, 1, 0],
        error=None,
    )


def test_controller_width_and_entire_request_use_custom_window():
    settings = timing()
    tracker = PulseTracker(settings["controllers"], settings)
    assert not tracker.ingest(packet(2, width=600))["valid"]
    tracker.ingest(packet(4.9))
    tracker.ingest(packet(5.3))
    context = tracker.assess("left", 5, 5.2, 5.35)
    assert context["valid"]
    engine = Engine(settings, "controller")
    engine.start(0, WALL)
    assert engine.accept("voltage_left", 2, 5, 5.2, WALL, sync_context=context)["valid"]
    tracker.ingest(packet(6.8))
    tracker.ingest(packet(7.2))
    assert not tracker.assess("left", 6.9, 7, 7.25)["valid"]


def completed_store(tmp_path):
    store = SessionStore(tmp_path / "measurements.sqlite3")
    sid = store.create_session("hardware", default_settings(), WALL)
    store.write_batch(
        samples=[
            dict(session_id=sid, elapsed=1, wall_time=WALL, role="current", value=2, valid=True)
        ],
        summaries=[dict(session_id=sid, elapsed=1, wall_time=WALL)],
        events=[dict(session_id=sid, wall_time=WALL, message="event")],
        controls=[dict(session_id=sid, elapsed=1, wall_time=WALL, group="left", sequence=1)],
    )
    store.finish_session(sid, None, WALL)
    return store


def test_clear_history_removes_all_related_rows_and_allows_new_experiment(tmp_path):
    store = completed_store(tmp_path)
    assert store.clear_history() == 1
    with sqlite3.connect(store.db_path) as connection:
        for table in ("sessions", "samples", "summaries", "events", "control_observations"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store.clear_history() == 0
    sid = store.create_session("hardware", default_settings(), WALL)
    assert store.get_session(sid)["status"] == "running"


def test_clear_refuses_running_experiment_and_rolls_back_on_failure(tmp_path):
    store = completed_store(tmp_path)
    sid = store.create_session("hardware", default_settings(), WALL)
    with pytest.raises(StorageError, match="运行"):
        store.clear_history()
    assert len(store.list_sessions()) == 2
    store.finish_session(sid, None, WALL)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "CREATE TRIGGER prevent_clear BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT, 'locked history'); END"
        )
    with pytest.raises(StorageError):
        store.clear_history()
    assert len(store.list_sessions()) == 2
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_settings_controls_preserve_and_restore_current_defaults(qtbot):
    dialog = SettingsDialog(timing())
    qtbot.addWidget(dialog)
    assert dialog.timing_inputs["step_seconds"].value() == 10
    dialog.timing_inputs["valid_start"].setValue(4)
    assert dialog.values()["valid_start"] == 4
    dialog.restore_timing_button.click()
    assert [dialog.values()[key] for key in ("step_seconds", "valid_start", "valid_end")] == [
        6,
        2,
        4,
    ]
    assert "6" in dialog.timing_note.text()


def test_clear_history_cancel_and_confirm_refreshes_all_views(qtbot, tmp_path, monkeypatch):
    store = completed_store(tmp_path)
    dialog = HistoryDialog(store)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    dialog.clear_button.click()
    assert len(store.list_sessions()) == 1
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    with qtbot.waitSignal(dialog.history_cleared, timeout=3000):
        dialog.clear_button.click()
    qtbot.waitUntil(lambda: dialog.clear_thread is None)
    assert dialog.session_id is None
    assert dialog.session_table.rowCount() == dialog.sample_table.rowCount() == 0
    assert not dialog.export_button.isEnabled()


def test_clear_is_disabled_while_acquiring(qtbot, tmp_path):
    store = completed_store(tmp_path)
    dialog = HistoryDialog(store, allow_clear=False)
    qtbot.addWidget(dialog)
    assert not dialog.clear_button.isEnabled()


def test_history_clear_resets_main_page_and_stopped_export_state(qtbot, tmp_path):
    settings = default_settings()
    window = MainWindow(settings)
    qtbot.addWidget(window)
    controller = Controller(window, settings, tmp_path, launch_workers=False)
    controller.start()
    controller.stop()
    assert controller.engine is not None
    controller.store.clear_history()
    controller.history_cleared()
    assert controller.engine is controller.last_snapshot is controller.session_id is None
    assert window.left_table.item(0, 1).text() == "—"
    controller.start()
    assert controller.running
    controller.stop()


def test_footer_uses_current_timing(qtbot):
    window = MainWindow(timing())
    qtbot.addWidget(window)
    assert "10" in window.footer_label.text() and "7" in window.footer_label.text()


def test_custom_timing_runs_through_controller_and_persists_and_exports(qtbot, tmp_path):
    settings = timing()
    window = MainWindow(settings)
    qtbot.addWidget(window)
    controller = Controller(window, settings, tmp_path, launch_workers=False)
    controller.start()
    controller.timer.stop()

    class Clock:
        def now(self):
            return 5.4

    controller.clock = Clock()
    for group, channel in (("left", 2), ("right", 9)):
        for t in (4.9, 5.3):
            observation = packet(t)
            observation.update(kind="controller", group=group)
            observation["registers"][0] = channel
            controller.output.put(observation)
    for role, value in (("voltage_left", 2), ("voltage_right", 3), ("current", 100)):
        controller.output.put(
            dict(kind="reading", role=role, value=value, started=5, finished=5.2, error=None)
        )
    controller.drain()
    controller.render()
    assert controller.last_snapshot["metrics"]["total_voltage"] == 5
    assert controller.last_snapshot["metrics"]["power_kw"] == pytest.approx(0.5)
    controller.stop()
    sid = controller.session_id
    assert controller.store.get_session(sid)["settings"] == settings
    assert {(s["cell"], s["valid"]) for s in controller.store.query_samples(sid)} == {
        (2, True),
        (24, True),
        (None, True),
    }
    assert len(controller.store.query_controls(sid)) == 4
    destination = tmp_path / "history.txt"
    controller.store.export_session(sid, destination)
    with destination.open(encoding="utf-8-sig", newline="") as stream:
        metadata = {row[0]: row[1] for row in csv.reader(stream, delimiter="\t") if len(row) == 2}
    assert json.loads(metadata["settings"]) == settings


def test_history_clear_handles_list_failure_without_crashing(qtbot, tmp_path, monkeypatch):
    store = completed_store(tmp_path)
    dialog = HistoryDialog(store)
    qtbot.addWidget(dialog)

    def fail():
        raise StorageError("database unavailable")

    monkeypatch.setattr(store, "list_sessions", fail)
    dialog.clear_history()
    assert "失败" in dialog.status_label.text()


def test_invalid_window_keeps_settings_dialog_open(qtbot):
    dialog = SettingsDialog(default_settings())
    qtbot.addWidget(dialog)
    dialog.timing_inputs["valid_end"].setValue(7)
    dialog.accept()
    assert "有效时间" in dialog.error_label.text()
    assert dialog.result() == 0


def test_selected_history_row_remains_legible(qtbot, tmp_path):
    dialog = HistoryDialog(completed_store(tmp_path))
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(100)
    table = dialog.session_table
    rect = table.visualItemRect(table.item(0, 0))
    image = table.viewport().grab(rect.adjusted(4, 4, -4, -4)).toImage()
    dark_pixels = sum(
        image.pixelColor(x, y).lightness() < 150
        for x in range(image.width())
        for y in range(image.height())
    )
    assert dark_pixels > 50, "选中的实验行必须能看到文字"
