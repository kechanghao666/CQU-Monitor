import math
import struct
from queue import Queue
import threading

import pytest

from cqu_monitor.config import default_settings, load_settings, save_settings, validate_settings
from cqu_monitor.workers import (
    decode_current,
    decode_voltage,
    next_deadline,
    Producer,
    ExperimentClock,
)
from cqu_monitor.workers import SerialProducer


def test_defaults_are_independent():
    first = default_settings()
    first["ports"]["current"]["port"] = "COM7"
    assert default_settings()["ports"]["current"]["port"] == "COM3"


def test_hardware_ports_required_only_before_start():
    settings = default_settings()
    settings["mode"] = "hardware"
    validate_settings(settings)
    for port in settings["ports"].values():
        port["port"] = ""
    with pytest.raises(ValueError, match="串口"):
        validate_settings(settings, for_start=True)
    for port in settings["ports"].values():
        port["port"] = "COM1"
    with pytest.raises(ValueError, match="重复"):
        validate_settings(settings, for_start=True)


@pytest.mark.parametrize(
    "key,value",
    [("voltage_low", math.nan), ("mode", "simulation"), ("sample_interval", 2), ("valid_end", 7)],
)
def test_bad_settings(key, value):
    settings = default_settings()
    settings[key] = value
    with pytest.raises(ValueError):
        validate_settings(settings)


def test_zero_threshold_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    settings = default_settings()
    settings["voltage_low"] = 0
    save_settings(path, settings)
    assert load_settings(path)["voltage_low"] == 0


def test_corrupt_settings_does_not_silently_overwrite(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="配置"):
        load_settings(path)
    assert path.read_text(encoding="utf-8") == "{broken"


def test_voltage_big_endian_first_two_registers():
    words = struct.unpack(">HH", struct.pack(">f", 1.875))
    assert decode_voltage([*words, 0xFFFF, 0xFFFF]) == pytest.approx(1.875)


@pytest.mark.parametrize("registers", [[], [1], [0x7F80, 0, 0, 0], [70000, 0, 0, 0]])
def test_voltage_rejects_bad_frames(registers):
    with pytest.raises(ValueError):
        decode_voltage(registers)


@pytest.mark.parametrize("raw,expected", [(0, 0), (5000, 125), (10000, 250)])
def test_current_scale(raw, expected):
    assert decode_current([raw]) == pytest.approx(expected)


@pytest.mark.parametrize("registers", [[], [10001], [-1]])
def test_current_rejects_wrong_range(registers):
    with pytest.raises(ValueError):
        decode_current(registers)


def test_deadlines_skip_missed_reads_without_burst_or_phase_drift():
    assert next_deadline(0.5, 0.8, 1) == 1.5
    assert next_deadline(0.5, 3.2, 1) == 3.5
    assert next_deadline(0.5, 3.5, 1) == 4.5


def test_full_queue_preserves_completed_reading():
    output, stop = Queue(maxsize=1), threading.Event()
    output.put({"kind": "device"})
    producer = Producer(output, ExperimentClock(), stop, "test")
    reading = {"kind": "reading", "value": 2}
    producer.publish(reading)
    assert producer.pending == [reading]
    assert stop.is_set()


def test_early_wait_return_cannot_trigger_duplicate_read_before_deadline():
    class Clock:
        value = 0

        def now(self):
            return self.value

    clock = Clock()

    class Stop:
        calls = 0

        def is_set(self):
            return False

        def wait(self, delay):
            self.calls += 1
            clock.value = 0.4 if self.calls == 1 else 0.5
            return False

    stop = Stop()
    producer = Producer(Queue(), clock, stop, "early-wait")
    assert not producer.wait_until(0.5)
    assert clock.now() >= 0.5 and stop.calls == 2


def test_serial_request_times_exclude_connect_delay_and_preserve_last_read():
    class Clock:
        speed = 1
        value = 1.5

        def now(self):
            return self.value

    clock, stop, output = Clock(), threading.Event(), Queue()

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["parity"] == "N"
            assert kwargs["retries"] == 0

        def connect(self):
            clock.value = 2.5
            return True

        def read_holding_registers(self, address, count, unit):
            assert (address, count, unit) == (0x2A, 4, 1)
            clock.value = 2.6
            stop.set()

            class Response:
                registers = [0x3FF0, 0, 0, 0]

                def isError(self):
                    return False

            return Response()

        def close(self):
            pass

    worker = SerialProducer(
        "voltage_left", default_settings()["ports"]["voltage_left"], output, clock, stop, Client
    )
    worker.wait_until = lambda deadline: stop.is_set()
    worker.start()
    worker.join(2)
    rows = list(output.queue)
    reading = next(row for row in rows if row["kind"] == "reading")
    assert reading["started"] == 2.5
    assert reading["finished"] == 2.6
    assert reading["value"] == pytest.approx(1.875)
    assert not worker.is_alive()
