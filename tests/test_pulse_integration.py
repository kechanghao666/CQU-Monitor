from PySide6.QtCore import QObject, Signal
import pytest

from cqu_monitor.config import default_settings
from cqu_monitor.controller import Controller


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
        self.samples = []
        self.events = []

    def set_devices(self, *args):
        pass

    def set_running(self, *args, **kwargs):
        pass

    def reset_session(self, *args):
        self.samples.clear()

    def add_sample(self, s):
        self.samples.append(s)

    def add_summary(self, s):
        pass

    def render(self, s):
        self.snapshot = s

    def append_event(self, *args):
        self.events.append(args)


class Clock:
    def __init__(self, now):
        self.value = now

    def now(self):
        return self.value


def packet(group, t, channel, remaining, error=None):
    return dict(
        kind="controller",
        group=group,
        started=t,
        finished=t + 0.01,
        parameters=[600, 1, 1, 15],
        registers=[channel, round(remaining * 100), 0, 0, 3, 0],
        error=error,
    )


def reading(role, value):
    return dict(kind="reading", role=role, value=value, started=2.5, finished=2.51, error=None)


@pytest.fixture
def controller(qapp, tmp_path):
    c = Controller(Window(), default_settings(), tmp_path, launch_workers=False)
    c.start()
    c.clock = Clock(0)
    yield c
    c.shutdown()


def test_independent_channels_sample_arrives_before_controller_queue(controller):
    c = controller
    c.output.put(reading("voltage_left", 1.8))
    c.output.put(reading("voltage_right", 1.9))
    c.output.put(reading("current", 100))
    for group, channel in [("left", 5), ("right", 12)]:
        c.output.put(packet(group, 2.3, channel, 3.7))
        c.output.put(packet(group, 2.7, channel, 3.3))
    c.clock.value = 2.8
    c.drain()
    readings = {s["role"]: s for s in c.window.samples}
    assert readings["voltage_left"]["valid"] and readings["voltage_left"]["cell"] == 5
    assert readings["voltage_right"]["valid"] and readings["voltage_right"]["cell"] == 27
    c.render()
    assert c.last_snapshot["controllers"]["left"]["cell"] == 5
    assert c.last_snapshot["controllers"]["right"]["cell"] == 27
    assert c.last_snapshot["metrics"]["total_voltage"] == pytest.approx(3.7)
    c.stop()
    assert len(c.store.query_controls(c.session_id)) == 4
    assert len(c.store.query_samples(c.session_id)) == 3


def test_pending_voltage_times_out_without_pausing_current(controller):
    c = controller
    c.output.put(reading("voltage_left", 1.8))
    c.output.put(reading("current", 100))
    c.clock.value = 2.8
    c.drain()
    assert len(c.window.samples) == 1 and c.window.samples[0]["role"] == "current"
    c.clock.value = 3.7
    c.drain()
    assert len(c.window.samples) == 2
    assert not c.window.samples[-1]["valid"]
    assert c.window.samples[-1]["cell"] is None
    assert c.engine.snapshot(3.7)["metrics"]["current"] == 100


def test_left_error_does_not_invalidate_right_and_tail_is_saved(controller):
    c = controller
    for group, channel in [("left", 5), ("right", 12)]:
        c.output.put(packet(group, 2.3, channel, 3.7))
        if group == "left":
            c.output.put(packet(group, 2.6, channel, 3.4, error="断线"))
        c.output.put(packet(group, 2.7, channel, 3.3))
    c.output.put(reading("voltage_left", 1.8))
    c.output.put(reading("voltage_right", 1.9))
    c.clock.value = 2.8
    c.drain()
    samples = {s["role"]: s for s in c.window.samples}
    assert not samples["voltage_left"]["valid"]
    assert samples["voltage_right"]["valid"]
    c.output.put(
        dict(kind="reading", role="voltage_left", value=2, started=3.5, finished=3.51, error=None)
    )
    c.clock.value = 3.6
    c.stop()
    stored = c.store.query_samples(c.session_id)
    assert len(stored) == 3 and len(c.store.query_controls(c.session_id)) == 5
    assert not stored[-1]["valid"]


def test_manual_sync_signal_is_ignored_in_controller_mode(controller):
    c = controller
    c.synchronize()
    assert c.engine._sync is None
    assert not c.window.events or all("人工同步：" not in str(e) for e in c.window.events)


def test_stop_loads_all_worker_pending_evidence_before_voltage_decision(controller):
    c = controller
    c.output.put(packet("left", 2.3, 5, 3.7))
    c.output.put(reading("voltage_left", 1.8))

    class DeadWorker:
        pending = [packet("left", 2.7, 5, 3.3)]

        def join(self, timeout):
            pass

        def is_alive(self):
            return False

    c.workers = [DeadWorker()]
    c.clock.value = 4
    c.stop()
    rows = c.store.query_samples(c.session_id)
    assert len(rows) == 1 and rows[0]["valid"]
    assert len(c.store.query_controls(c.session_id)) == 2


def test_poll_failure_joins_pending_controls_before_final_voltage_decision(controller):
    c = controller
    c.output.put(packet("left", 2.3, 5, 3.7))
    c.output.put(reading("voltage_left", 1.8))

    class FailedWorker:
        pending = [packet("left", 2.7, 5, 3.3)]
        failure = "读取结果队列已满"

        def join(self, timeout):
            pass

        def is_alive(self):
            return False

    c.workers = [FailedWorker()]
    c.clock.value = 4
    c.poll()
    rows = c.store.query_samples(c.session_id)
    assert not c.running
    assert len(rows) == 1 and rows[0]["valid"]
    assert len(c.store.query_controls(c.session_id)) == 2
