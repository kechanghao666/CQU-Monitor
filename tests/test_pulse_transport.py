from queue import Queue
from threading import Event

import pytest

from cqu_monitor.pulse_workers import PulseBusProducer


class Clock:
    def __init__(self):
        self.value = 0

    def now(self):
        self.value += 0.001
        return self.value


class Reply:
    registers = [600, 0, 0, 15]

    def isError(self):
        return False


class Client:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.closed = False

    def connect(self):
        return True

    def close(self):
        self.closed = True

    def read_holding_registers(self, address, count, unit):
        self.calls.append((3, address, count, unit))
        return Reply()

    def read_input_registers(self, address, count, unit):
        self.calls.append((4, address, count, unit))
        return Reply()


def profile(unit=1, **changes):
    result = dict(
        port="COM4",
        baudrate=9600,
        unit=unit,
        parity="E",
        stopbits=1,
        timeout=0.1,
        function=3,
        address_offset=0,
        countdown_scale=0.01,
    )
    result.update(changes)
    return result


@pytest.mark.parametrize("function,offset", [(3, 0), (4, -1)])
def test_read_group_is_two_readonly_requests_with_whole_pair_timestamps(function, offset):
    worker = PulseBusProducer(
        {"left": profile(function=function, address_offset=offset)}, Queue(), Clock(), Event()
    )
    client = Client()
    result = worker.read_group(client, "left")
    assert client.calls == [(function, 11 + offset, 4, 1), (function, 22 + offset, 6, 1)]
    assert result["kind"] == "controller"
    assert result["group"] == "left"
    assert result["finished"] > result["started"]
    assert result["error"] is None


def test_shared_bus_uses_one_client_sequential_units_and_closes_on_stop():
    output, stop, clients = Queue(), Event(), []

    def factory(**kwargs):
        client = Client(**kwargs)
        original = client.read_holding_registers

        def read(address, count, unit):
            reply = original(address, count, unit)
            if unit == 2 and count == 6:
                stop.set()
            return reply

        client.read_holding_registers = read
        clients.append(client)
        return client

    worker = PulseBusProducer(
        {"left": profile(), "right": profile(2)}, output, Clock(), stop, client_factory=factory
    )
    worker.wait_until = lambda deadline: stop.is_set()
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(clients) == 1
    assert clients[0].calls == [(3, 11, 4, 1), (3, 22, 6, 1), (3, 11, 4, 2), (3, 22, 6, 2)]
    assert clients[0].kwargs["parity"] == "E"
    assert clients[0].kwargs["retries"] == 0
    assert clients[0].closed
    packets = list(output.queue)
    assert [p["group"] for p in packets if p["kind"] == "controller"] == ["left", "right"]
    assert worker.failure is None


def test_incompatible_physical_bus_profiles_are_rejected():
    with pytest.raises(ValueError):
        PulseBusProducer(
            {"left": profile(), "right": profile(2, parity="N")}, Queue(), Clock(), Event()
        )


def test_read_failure_keeps_partial_raw_parameters_and_is_reconnected():
    output, stop, clients = Queue(), Event(), []

    def factory(**kwargs):
        client = Client(**kwargs)
        original = client.read_holding_registers

        def read(address, count, unit):
            if not clients[:-1]:
                if count == 6:
                    raise TimeoutError("test timeout")
            else:
                stop.set()
            return original(address, count, unit)

        client.read_holding_registers = read
        clients.append(client)
        return client

    worker = PulseBusProducer({"left": profile()}, output, Clock(), stop, factory)
    worker.wait_until = lambda deadline: stop.is_set()
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    packets = [packet for packet in output.queue if packet["kind"] == "controller"]
    assert len(packets) == 2
    assert packets[0]["parameters"] == [600, 0, 0, 15]
    assert packets[0]["registers"] is None
    assert "TimeoutError" in packets[0]["error"]
    assert packets[1]["error"] is None
    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_queue_failure_preserves_pending_and_stops_before_opening_port():
    output, stop, clients = Queue(maxsize=1), Event(), []
    output.put({"occupied": True})

    def factory(**kwargs):
        clients.append(Client(**kwargs))
        return clients[-1]

    worker = PulseBusProducer({"left": profile()}, output, Clock(), stop, factory)
    worker.wait_until = lambda deadline: stop.is_set()
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert stop.is_set()
    assert worker.failure is not None
    assert worker.pending[0]["state"] == "connecting"
    assert clients == []
