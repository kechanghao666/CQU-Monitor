"""停止应保持界面响应；阻塞串口进程必须可回收，尾部结果仍须落盘。"""

from queue import Queue
import threading
import time

from PySide6.QtCore import QTimer
import pytest

from cqu_monitor.config import default_settings
from cqu_monitor.controller import Controller
from cqu_monitor.ui import MainWindow
from cqu_monitor.workers import ExperimentClock


class BlockedDevice:
    def __init__(self, output, clock, stop_event):
        self.output, self.stop_event = output, stop_event
        self.failure = None
        self.pending = []

    def run(self):
        self.output.put({"kind": "test", "number": 1})
        time.sleep(60)


class FinishingDevice(BlockedDevice):
    def run(self):
        self.output.put({"kind": "test", "number": 1})
        self.stop_event.wait(20)
        self.output.put({"kind": "test", "number": 2})


class BlockedMeter(BlockedDevice):
    def __init__(self, output, clock, stop_event):
        super().__init__(output, clock, stop_event)
        self.clock = clock

    def run(self):
        now = self.clock.now()
        self.output.put(
            dict(kind="reading", role="current", value=100, started=now, finished=now, error=None)
        )
        time.sleep(60)


class FailedDevice(BlockedDevice):
    def run(self):
        raise RuntimeError("test-only process crash")


@pytest.mark.parametrize(
    "device,forced,count", [(BlockedDevice, True, 1), (FinishingDevice, False, 2)]
)
def test_isolated_device_stop_is_bounded_and_preserves_published_packets(device, forced, count):
    from cqu_monitor.isolated import IsolatedProducer

    output, stop = Queue(), threading.Event()
    worker = IsolatedProducer(
        device, (), output, ExperimentClock(), stop, name="test-io", stop_grace=0.3
    )
    worker.start()
    try:
        first = output.get(timeout=8)
        assert first["number"] == 1
        started = time.monotonic()
        stop.set()
        worker.join(3)
        assert not worker.is_alive()
        assert time.monotonic() - started < 2
        assert worker.forced_stop is forced
        assert not worker.process.is_alive()
        packets = [first]
        while not output.empty():
            packets.append(output.get_nowait())
        assert len(packets) == count
    finally:
        stop.set()
        worker.join(3)


def test_stop_button_returns_and_heartbeat_runs_while_reader_exits(qtbot, tmp_path):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    controller = Controller(window, default_settings(), tmp_path, launch_workers=False)
    controller.start()
    released = threading.Event()

    class SlowReader:
        pending = []
        failure = None

        def join(self, timeout):
            released.wait(0.3)

        def is_alive(self):
            return not released.is_set()

    controller.workers = [SlowReader()]
    ticks = []
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: ticks.append(1))
    heartbeat.start(10)
    try:
        # 先确认事件循环已经进入稳定计时；前序窗口的延迟销毁不属于停止耗时。
        qtbot.waitUntil(lambda: len(ticks) >= 2, timeout=2000)
        ticks.clear()
        start = time.monotonic()
        window.stop_button.click()
        assert time.monotonic() - start < 0.15
        assert "正在停止" in window.run_label.text()
        assert not window.start_button.isEnabled() and not window.stop_button.isEnabled()
        qtbot.wait(120)
        assert len(ticks) > 3
        assert controller.stopping
    finally:
        released.set()
        qtbot.waitUntil(lambda: not controller.running, timeout=3000)
        heartbeat.stop()
    assert controller.store.get_session(controller.session_id)["status"] == "completed"


def test_slow_database_finish_does_not_freeze_stop_button(qtbot, tmp_path, monkeypatch):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    controller = Controller(window, default_settings(), tmp_path, launch_workers=False)
    controller.start()
    entered, release = threading.Event(), threading.Event()
    original = controller.store.finish_session

    def slow_finish(*args, **kwargs):
        entered.set()
        release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(controller.store, "finish_session", slow_finish)
    try:
        start = time.monotonic()
        window.stop_button.click()
        assert time.monotonic() - start < 0.15
        qtbot.waitUntil(entered.is_set)
        assert controller.stopping
        qtbot.wait(60)
    finally:
        release.set()
        qtbot.waitUntil(lambda: not controller.running, timeout=3000)
    assert not controller.recorder.alive


def test_close_waits_asynchronously_for_tail_data_then_closes(qtbot, tmp_path):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    controller = Controller(window, default_settings(), tmp_path, launch_workers=False)
    window.show()
    controller.start()
    controller.output.put(
        dict(kind="reading", role="current", value=100, started=0.1, finished=0.2, error=None)
    )
    start = time.monotonic()
    window.close()
    assert time.monotonic() - start < 0.15
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=3000)
    assert not controller.running
    assert len(controller.store.query_samples(controller.session_id)) == 1


def test_actual_process_hang_stops_saves_tail_and_can_restart(qtbot, tmp_path):
    from cqu_monitor.isolated import IsolatedProducer

    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    controller = Controller(window, default_settings(), tmp_path, launch_workers=False)
    controller.start()
    worker = IsolatedProducer(
        BlockedMeter,
        (),
        controller.output,
        controller.clock,
        controller.stop_event,
        name="blocked-meter",
        stop_grace=0.3,
    )
    controller.workers = [worker]
    worker.start()
    try:
        qtbot.waitUntil(lambda: window.metric_values["current"].text() == "100.000", timeout=8000)
        first = controller.session_id
        window.stop_button.click()
        qtbot.waitUntil(lambda: not controller.running, timeout=3000)
        assert not worker.is_alive() and not worker.process.is_alive()
        assert controller.store.get_session(first)["status"] == "interrupted"
        assert len(controller.store.query_samples(first)) == 1
        assert controller.recorder.pending_count == 0
        controller.start()
        assert controller.running and controller.session_id != first
        controller.stop()
    finally:
        controller.stop_event.set()
        worker.join(3)
        controller.stop()


def test_child_crash_is_visible_and_stops_production():
    from cqu_monitor.isolated import IsolatedProducer

    stop = threading.Event()
    worker = IsolatedProducer(FailedDevice, (), Queue(), ExperimentClock(), stop, name="crash")
    worker.start()
    worker.join(8)
    assert not worker.is_alive() and stop.is_set()
    assert "test-only process crash" in worker.failure


def test_full_parent_queue_keeps_every_transferred_tail_packet():
    from cqu_monitor.isolated import IsolatedProducer

    output, stop = Queue(maxsize=1), threading.Event()
    output.put({"number": 0})
    worker = IsolatedProducer(
        FinishingDevice, (), output, ExperimentClock(), stop, name="full-queue", stop_grace=0.3
    )
    worker.start()
    worker.join(8)
    try:
        assert not worker.is_alive()
        assert [packet["number"] for packet in worker.pending] == [1, 2]
        assert output.get_nowait()["number"] == 0
    finally:
        stop.set()
        worker.join(3)
