"""全面审计发现的异常路径、参数边界与曲线证据回归。"""

import math
import json
import sqlite3
import threading
import time

import pytest

from cqu_monitor.config import default_settings, validate_settings
from cqu_monitor.controller import Controller
from cqu_monitor.ui import MainWindow
from cqu_monitor.storage import SessionStore, QueuedRecorder, StorageError
from test_controller import Window, default_settings as manual_settings


class Reader:
    def __init__(self):
        self.pending = []
        self.failure = None
        self.alive = True
        self.forced_stop = False
        self.name = "late-reader"

    def is_alive(self):
        return self.alive


def test_failed_recovery_rewrite_preserves_previous_complete_file(tmp_path, monkeypatch):
    import cqu_monitor.storage as storage

    store = SessionStore(tmp_path / "recovery.sqlite3")
    sid = store.create_session("hardware", default_settings(), "2026-10-16T00:00:00+08:00")
    recorder = QueuedRecorder(store, sid)
    recorder.start()
    recorder._record_failure(OSError("first failure"))
    original = recorder.recovery_path.read_bytes()

    def disk_fail(_fd):
        raise OSError("recovery fsync failure")

    monkeypatch.setattr(storage.os, "fsync", disk_fail)
    with pytest.raises(StorageError):
        recorder.close(dict(session_id=sid), "2026-10-16T01:00:00+08:00")
    assert recorder.recovery_path.read_bytes() == original


def test_late_worker_failure_is_saved_as_error(qtbot, tmp_path):
    c = Controller(Window(), manual_settings(), tmp_path, launch_workers=False)
    c.start()
    reader = Reader()
    c.workers = [reader]
    c.request_stop()
    reader.failure = "关闭串口失败：injected close failure"
    reader.forced_stop = True
    reader.alive = False
    qtbot.waitUntil(lambda: not c.running)
    assert c.store.get_session(c.session_id)["status"] == "error"
    assert "injected close failure" in c.last_error
    with sqlite3.connect(c.store.db_path) as connection:
        events = connection.execute(
            "SELECT payload FROM events WHERE session_id=?", (c.session_id,)
        ).fetchall()
    assert any("injected close failure" in json.loads(row[0])["message"] for row in events)


def test_tail_ui_failure_cannot_finish_while_reader_alive(qtbot, tmp_path):
    c = Controller(Window(), manual_settings(), tmp_path, launch_workers=False)
    c.start()
    reader = Reader()
    c.workers = [reader]
    for index, value in enumerate((100, 101)):
        c.output.put(
            dict(
                kind="reading",
                role="current",
                value=value,
                started=0.1 + index,
                finished=0.2 + index,
                error=None,
            )
        )

    def fail_display(_sample):
        raise RuntimeError("injected tail display failure")

    c.window.add_sample = fail_display
    try:
        c.request_stop()
        qtbot.wait(100)
        assert c.running and c.stopping and c.recorder.alive
        assert c.store.get_session(c.session_id)["status"] == "running"
        first = c.session_id
        c.start()
        assert c.session_id == first
    finally:
        reader.alive = False
        qtbot.waitUntil(lambda: not c.running)
    assert [s["value"] for s in c.store.query_samples(first)] == [100, 101]
    assert c.store.get_session(first)["status"] == "error"


def test_slow_recovery_file_does_not_lock_stop_entry(qtbot, tmp_path, monkeypatch):
    c = Controller(Window(), manual_settings(), tmp_path, launch_workers=False)
    c.start()
    entered, release = threading.Event(), threading.Event()
    original = c.recorder._save_recovery

    def slow_recovery(*args, **kwargs):
        entered.set()
        release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(c.recorder, "_save_recovery", slow_recovery)
    thread = threading.Thread(target=c.recorder._record_failure, args=(OSError("test disk error"),))
    thread.start()
    assert entered.wait(2)
    try:
        start = time.monotonic()
        c.request_stop()
        assert time.monotonic() - start < 0.15
        assert "test disk error" in c.recorder.error
    finally:
        release.set()
        thread.join(3)
        qtbot.waitUntil(lambda: not c.running, timeout=4000)
    assert c.recorder.recovery_path.exists()


@pytest.mark.parametrize("baud", [1200, 2400])
def test_controller_baudrate_that_cannot_meet_snapshot_span_is_rejected(baud):
    s = default_settings()
    s["controllers"]["left"]["baudrate"] = baud
    with pytest.raises(ValueError, match="4800|过低"):
        validate_settings(s, for_start=True)


def test_voltage_fault_breaks_curve_but_normal_window_does_not(qtbot):
    w = MainWindow(default_settings())
    qtbot.addWidget(w)
    chart = w.cell_charts[1]
    w.add_sample(
        dict(
            role="voltage_left",
            cell=1,
            elapsed=2.5,
            value=2,
            mean_value=2,
            valid=True,
            status="valid",
        )
    )
    w.add_sample(
        dict(
            role="voltage_left",
            cell=1,
            elapsed=4.5,
            value=2,
            mean_value=2,
            valid=False,
            status="outside_window",
        )
    )
    assert len(chart.times) == 1
    for second in (7.5, 8.5):
        w.add_sample(
            dict(
                role="voltage_left",
                cell=1,
                elapsed=second,
                value=None,
                mean_value=2,
                valid=False,
                status="error",
            )
        )
    assert len(chart.times) == 2 and math.isnan(chart.latest_values[-1])
    w.add_sample(
        dict(
            role="voltage_left",
            cell=1,
            elapsed=92.5,
            value=3,
            mean_value=2.5,
            valid=True,
            status="valid",
        )
    )
    assert math.isnan(chart.latest_values[-2])


def test_unknown_voltage_fault_breaks_only_its_group(qtbot):
    w = MainWindow(default_settings())
    qtbot.addWidget(w)
    for cell, role in ((1, "voltage_left"), (16, "voltage_right")):
        w.add_sample(
            dict(
                role=role, cell=cell, elapsed=2.5, value=2, mean_value=2, valid=True, status="valid"
            )
        )
    w.add_sample(
        dict(role="voltage_left", cell=None, elapsed=3.5, value=None, valid=False, status="error")
    )
    assert math.isnan(w.cell_charts[1].latest_values[-1])
    assert w.cell_charts[16].latest_values[-1] == 2


def test_measurement_tooltip_respects_custom_window(qtbot):
    s = default_settings()
    s.update(step_seconds=10, valid_start=3, valid_end=7)
    w = MainWindow(s)
    qtbot.addWidget(w)
    w.set_running(True)
    w._set_measurement_indicators(
        controllers={"left": dict(active=True, cell=1), "right": dict(active=False)}
    )
    assert "中间两秒" not in w.current_measurement_label.toolTip()
    assert (
        "3" in w.current_measurement_label.toolTip()
        and "7" in w.current_measurement_label.toolTip()
    )


def test_normal_controller_intervals_do_not_report_false_staleness(qtbot, tmp_path):
    c = Controller(Window(), default_settings(), tmp_path, launch_workers=False)
    c.start()
    c.timer.stop()
    for t, remaining in ((2.3, 370), (2.7, 330)):
        c.output.put(
            dict(
                kind="controller",
                group="left",
                started=t,
                finished=t + 0.01,
                parameters=[600, 1, 1, 15],
                registers=[1, remaining, 0, 0, 1, 0],
                error=None,
            )
        )
    c.output.put(
        dict(kind="reading", role="voltage_left", value=2, started=2.5, finished=2.51, error=None)
    )
    c.clock.now = lambda: 2.8
    c.drain()
    c.engine.set_controller_health("left", True)
    assert c.engine.snapshot(103)["metrics"]["available_count"] == 1
    assert c.engine.snapshot(130)["metrics"]["available_count"] == 0
    c.stop()


def test_fractional_output_width_keeps_controller_wait_intervals(qtbot, tmp_path):
    settings = default_settings()
    settings.update(step_seconds=0.35, valid_start=0.1, valid_end=0.3)
    c = Controller(Window(), settings, tmp_path, launch_workers=False)
    c.start()
    c.timer.stop()
    for t, countdown in ((0.1, 25), (0.2, 15)):
        c.output.put(
            dict(
                kind="controller",
                group="left",
                started=t,
                finished=t + 0.01,
                parameters=[35, 1, 1, 15],
                registers=[1, countdown, 0, 0, 1, 0],
                error=None,
            )
        )
    c.output.put(
        dict(kind="reading", role="voltage_left", value=2, started=0.15, finished=0.16, error=None)
    )
    c.clock.now = lambda: 0.3
    c.drain()
    c.engine.set_controller_health("left", True)
    assert c.engine.snapshot(10)["metrics"]["available_count"] == 1
    c.stop()


def test_final_voltage_assessment_failure_keeps_rest_and_raw_packet(qtbot, tmp_path, monkeypatch):
    c = Controller(Window(), default_settings(), tmp_path, launch_workers=False)
    c.start()
    c.timer.stop()
    for t, countdown in ((2.3, 370), (2.7, 330)):
        c.output.put(
            dict(
                kind="controller",
                group="left",
                started=t,
                finished=t + 0.01,
                parameters=[600, 1, 1, 15],
                registers=[1, countdown, 0, 0, 1, 0],
                error=None,
            )
        )
    for t, value in ((2.5, 2), (2.55, 3)):
        c.output.put(
            dict(
                kind="reading",
                role="voltage_left",
                value=value,
                started=t,
                finished=t + 0.01,
                error=None,
            )
        )
    original = c.pulse.assess

    def fail_once(group, started, *args, **kwargs):
        if started == 2.5:
            raise RuntimeError("injected voltage assessment failure")
        return original(group, started, *args, **kwargs)

    monkeypatch.setattr(c.pulse, "assess", fail_once)
    c.clock.now = lambda: 3
    c.stop()
    assert not c.pending_voltage
    assert [r["value"] for r in c.store.query_samples(c.session_id)] == [3]
    assert c.store.get_session(c.session_id)["status"] == "error"
    recovered = [
        json.loads(line) for line in c.recovery_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        row["kind"] == "raw_packet" and "injected voltage assessment failure" in str(row)
        for row in recovered
    )
