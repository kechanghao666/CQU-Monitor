from datetime import datetime
import json

from PySide6.QtCore import QObject, Signal

from cqu_monitor.config import default_settings as base_settings
from cqu_monitor.controller import Controller


def default_settings():
    settings = base_settings()
    settings["synchronization"] = "manual"
    for index, port in enumerate(settings["ports"].values(), 1):
        port["port"] = f"COM{index}"
    return settings


class Window(QObject):
    start_requested = Signal()
    stop_requested = Signal()
    sync_requested = Signal()
    settings_requested = Signal()
    export_requested = Signal()
    history_requested = Signal()
    close_requested = Signal()

    def __init__(self):
        super().__init__()
        self.samples, self.events = [], []

    def set_running(self, *args, **kwargs):
        pass

    def render(self, snapshot):
        self.snapshot = snapshot

    def set_devices(self, statuses):
        pass

    def append_event(self, *args):
        self.events.append(args)

    def reset_session(self, mode):
        self.samples.clear()

    def add_sample(self, sample):
        self.samples.append(sample)

    def add_summary(self, summary):
        pass


def test_stop_drains_final_samples_and_restart_is_new_experiment(qapp, tmp_path):
    window = Window()
    controller = Controller(window, default_settings(), tmp_path, launch_workers=False)
    controller.start()
    first = controller.session_id
    controller.engine.synchronize(0)
    for role in ("voltage_left", "voltage_right", "current"):
        controller.output.put(
            dict(
                kind="reading",
                role=role,
                value=2 if role != "current" else 100,
                started=2.5,
                finished=2.51,
                error=None,
            )
        )
    controller.stop()
    rows = controller.store.query_samples(first)
    assert len(rows) == 3
    assert all(row["valid"] for row in rows)
    assert controller.store.get_session(first)["status"] == "completed"
    controller.start()
    assert controller.session_id != first
    assert all(cell["count"] == 0 for cell in controller.engine.snapshot(0)["cells"])
    controller.shutdown()
    assert len(controller.store.list_sessions()) == 2


def test_hardware_waits_for_manual_sync_without_opening_ports(qapp, tmp_path):
    settings = default_settings()
    settings["mode"] = "hardware"
    for index, port in enumerate(settings["ports"].values(), 1):
        port["port"] = f"COM{index}"
    window = Window()
    controller = Controller(window, settings, tmp_path, launch_workers=False)
    controller.start()
    assert not controller.engine.snapshot(0)["synchronized"]
    controller.output.put(
        dict(kind="reading", role="voltage_left", value=2, started=0, finished=0.001, error=None)
    )
    controller.drain()
    assert window.samples[-1]["status"] == "unsynchronized"
    controller.synchronize()
    assert controller.engine.snapshot(controller.clock.now())["synchronized"]
    controller.shutdown()


def test_recorded_wall_time_has_timezone(qapp, tmp_path):
    controller = Controller(Window(), default_settings(), tmp_path, launch_workers=False)
    controller.start()
    assert datetime.fromisoformat(controller.wall_time(12.5)).utcoffset() is not None
    controller.shutdown()


def test_rejected_tail_is_preserved_in_recovery(qapp, tmp_path, monkeypatch):
    controller = Controller(Window(), default_settings(), tmp_path, launch_workers=False)
    controller.start()

    def reject(*args):
        raise OSError("模拟磁盘拒绝")

    monkeypatch.setattr(controller.recorder, "submit", reject)
    for t in (2.5, 3.5):
        controller.output.put(
            dict(
                kind="reading", role="current", value=100, started=t, finished=t + 0.01, error=None
            )
        )
    controller.stop()
    rows = [
        json.loads(line)
        for line in controller.recovery_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len([row for row in rows if row["kind"] == "sample"]) == 2
    assert controller.store.get_session(controller.session_id)["status"] == "error"
    assert controller.last_error


def test_slow_reader_does_not_finish_session_or_allow_restart(qapp, tmp_path):
    class SlowReader:
        alive = True
        failure = None
        pending = []

        def join(self, timeout):
            pass

        def is_alive(self):
            return self.alive

    controller = Controller(Window(), default_settings(), tmp_path, launch_workers=False)
    controller.start()
    old_id = controller.session_id
    reader = SlowReader()
    controller.workers = [reader]
    controller.stop()
    try:
        assert controller.running
        assert controller.recorder.alive
        assert controller.store.get_session(old_id)["status"] == "running"
        controller.start()
        assert controller.session_id == old_id
    finally:
        reader.alive = False
        controller.stop()


def test_stopped_page_export_freezes_time_and_displayed_metrics(qapp, tmp_path):
    controller = Controller(Window(), default_settings(), tmp_path, launch_workers=False)
    controller.start()
    controller.engine.synchronize(0)
    for role in ("voltage_left", "current"):
        controller.output.put(
            dict(
                kind="reading",
                role=role,
                value=2 if role != "current" else 100,
                started=2.5,
                finished=2.51,
                error=None,
            )
        )
    controller.stop()
    original = controller.last_snapshot
    controller.clock.now = lambda: 300
    exported = controller.capture_snapshot()
    assert exported == original
    assert exported["metrics"]["current"] == 100
    assert exported["metrics"]["power_kw"] == 0.2


def test_two_round_hardware_pipeline_with_test_only_input(qapp, tmp_path):
    """测试夹具注入540条已知输入；发行版没有生成这些数值的入口。"""
    import math

    controller = Controller(Window(), default_settings(), tmp_path, launch_workers=False)
    controller.start()
    controller.timer.stop()
    controller.engine.synchronize(0)
    expected = {cell: [] for cell in range(1, 31)}
    try:
        for second in range(180):
            controller.clock.now = lambda t=second + 1: t
            for role in ("voltage_left", "voltage_right", "current"):
                cell = second // 6 % 15 + (16 if role == "voltage_right" else 1)
                value = (
                    100 + second / 100 if role == "current" else 1.7 + cell / 100 + second / 10000
                )
                controller.output.put(
                    dict(
                        kind="reading",
                        role=role,
                        value=value,
                        started=second + 0.5,
                        finished=second + 0.51,
                        error=None,
                    )
                )
                if role != "current" and second % 6 in (2, 3):
                    expected[cell].append(value)
            controller.poll()
        controller.stop()
        assert not controller.last_error
        rows = controller.store.query_samples(controller.session_id, limit=10000)
        assert len(rows) == 540
        assert sum(s["valid"] and s["role"] != "current" for s in rows) == 120
        assert sum(s["valid"] and s["role"] == "current" for s in rows) == 180
        for cell in controller.last_snapshot["cells"]:
            truth = expected[cell["cell"]]
            assert cell["count"] == 4 and math.isclose(cell["mean"], sum(truth) / 4)
        summaries = controller.store.query_summaries(controller.session_id, limit=1000)
        for raw, average in [("total_voltage", "avg_total_voltage"), ("power_kw", "avg_power_kw")]:
            values = [s[raw] for s in summaries if s[raw] is not None]
            assert math.isclose(
                controller.last_snapshot["metrics"][average], sum(values) / len(values)
            )
        assert not controller.recorder.alive and controller.recorder.pending_count == 0
    finally:
        controller.shutdown()


def test_edit_settings_keeps_stopped_results_consistent(qtbot, tmp_path, monkeypatch):
    from copy import deepcopy
    from PySide6.QtWidgets import QDialog
    from cqu_monitor.ui import MainWindow
    import cqu_monitor.dialogs as dialogs

    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    controller = Controller(window, default_settings(), tmp_path, launch_workers=False)
    controller.start()
    controller.engine.synchronize(0)
    controller.output.put(
        dict(kind="reading", role="voltage_left", value=2, started=2.5, finished=2.51, error=None)
    )
    controller.stop()
    snapshot = controller.capture_snapshot()
    changed = deepcopy(controller.settings)
    changed["ports"]["voltage_left"]["port"] = "COM8"

    class AcceptedSettings:
        def __init__(self, *args):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def values(self):
            return changed

        def deleteLater(self):
            pass

    monkeypatch.setattr(dialogs, "SettingsDialog", AcceptedSettings)
    controller.configure()
    assert window.left_table.item(0, 1).text() == "2.000"
    assert controller.capture_snapshot() == snapshot
    assert "COM8" in window.device_names["voltage_left"].text()
    controller.shutdown()
