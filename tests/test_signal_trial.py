"""模拟信号仅由外部测试工具提供，生产程序不包含数据发生器。"""

import threading
from queue import Queue
import pytest

from device_fixtures import SignalClient, TrialClock
from cqu_monitor.workers import decode_current, decode_voltage, SerialProducer
from cqu_monitor.config import default_settings


def test_virtual_voltage_and_current_follow_real_request_shapes():
    clock = TrialClock(1)
    clock.now = lambda: 2.5
    counts = dict(voltage_left=0, voltage_right=0, current=0)
    left = SignalClient("voltage_left", clock, counts)
    right = SignalClient("voltage_right", clock, counts)
    current = SignalClient("current", clock, counts)
    assert 1 < decode_voltage(left.read_holding_registers(0x2A, 4, unit=1).registers) < 3
    assert 1 < decode_voltage(right.read_holding_registers(0x2A, 4, unit=1).registers) < 3
    assert 100 < decode_current(current.read_holding_registers(0x56, 1, unit=1).registers) < 150
    with pytest.raises(ValueError):
        left.read_holding_registers(0x56, 1, unit=1)
    clock.now = lambda: 25.5
    assert current.read_holding_registers(0x56, 1, unit=1).isError()
    clock.now = lambda: 28.5
    assert not current.read_holding_registers(0x56, 1, unit=1).isError()


def test_simulated_reply_runs_through_production_serial_decoder():
    clock = TrialClock(1)
    clock.now = lambda: 2.5
    counts = dict(voltage_left=0, voltage_right=0, current=0)
    stop, output = threading.Event(), Queue()
    client = SignalClient("voltage_left", clock, counts)
    original_read = client.read_holding_registers

    def read(*args, **kwargs):
        stop.set()
        return original_read(*args, **kwargs)

    client.read_holding_registers = read
    worker = SerialProducer(
        "voltage_left",
        default_settings()["ports"]["voltage_left"],
        output,
        clock,
        stop,
        lambda **kwargs: client,
    )
    worker.wait_until = lambda deadline: stop.is_set()
    worker.start()
    worker.join(2)
    reading = next(row for row in list(output.queue) if row["kind"] == "reading")
    assert reading["error"] is None and 1 < reading["value"] < 3
    assert counts["voltage_left"] == 1 and not worker.is_alive()
