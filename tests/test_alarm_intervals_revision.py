"""正常控制器空档只暂停可靠指示；真实故障仍报警，采样窗口保持严格。"""

import pytest

from cqu_monitor.config import default_settings
from cqu_monitor.controller import Controller
from cqu_monitor.pulse import PulseTracker
from test_pulse_integration import Clock, Window, packet, reading
from test_pulse import profiles


@pytest.fixture
def controller(qapp, tmp_path):
    c = Controller(Window(), default_settings(), tmp_path, launch_workers=False)
    c.start()
    c.timer.stop()
    c.clock = Clock(0)
    yield c
    c.shutdown()


def waiting(group, t, *, cycle_wait=False):
    item = packet(group, t, 1, 0)
    item["parameters"] = [600, 1, 200 if cycle_wait else 1, 15]
    item["registers"] = [1, 0, 0, 0 if cycle_wait else 1, 3, 120 if cycle_wait else 0]
    return item


@pytest.mark.parametrize("cycle_wait", [False, True])
def test_slow_waiting_response_is_still_a_communication_fault(cycle_wait):
    tracker = PulseTracker(profiles())
    item = waiting("left", 2, cycle_wait=cycle_wait)
    item["finished"] = item["started"] + 0.3
    observation = tracker.ingest(item)
    assert observation["status"] == "slow_read"
    assert tracker.snapshot(item["finished"])["left"]["alarm"] is True


def show(c, now, *items):
    for item in items:
        c.output.put(item)
    c.clock.value = now
    c.drain()
    c.render()
    return c.last_snapshot


def seed_measured_voltage(c):
    return show(
        c,
        2.75,
        packet("left", 2.3, 1, 3.7),
        reading("voltage_left", 2),
        reading("current", 100),
        packet("left", 2.7, 1, 3.3),
        waiting("right", 2.7),
    )


def test_output_end_until_next_status_has_no_alarm_or_quality_degradation(controller):
    c = controller
    assert not seed_measured_voltage(c)["alarms"]
    current = dict(reading("current", 100), started=5.9, finished=5.91)
    snapshot = show(
        c,
        6.01,
        packet("left", 5.7, 1, 0.3),
        packet("left", 5.9, 1, 0.1),
        waiting("right", 5.9),
        current,
    )
    assert snapshot["controllers"]["left"]["active"] is False
    assert snapshot["controllers"]["left"]["status"] == "boundary_wait"
    assert snapshot["metrics"]["quality"] == "partial"
    assert snapshot["metrics"]["available_count"] == 1
    assert snapshot["alarms"] == []
    assert show(c, 6.12, waiting("left", 6.1))["alarms"] == []
    # 即使消除了误报警，跨输出边界的电压仍不得进入有效统计。
    context = c.pulse.assess("left", 5.91, 6.01, 6.12)
    assert context is not None and context["valid"] is False


@pytest.mark.parametrize("cycle_change", [False, True])
def test_first_status_of_channel_or_cycle_waits_for_confirmation_without_alarm(
    controller, cycle_change
):
    c = controller
    seed_measured_voltage(c)
    item = packet("left", 7, 1 if cycle_change else 2, 6)
    if cycle_change:
        item["registers"][4] += 1
    snapshot = show(
        c, 7.02, item, waiting("right", 7), dict(reading("current", 100), started=7, finished=7.01)
    )
    assert snapshot["controllers"]["left"]["active"] is False
    assert snapshot["alarms"] == []
    following = packet("left", 7.2, item["registers"][0], 5.8)
    following["registers"][4] = item["registers"][4]
    snapshot = show(c, 7.22, following)
    assert snapshot["controllers"]["left"]["active"] is True
    assert snapshot["alarms"] == []


@pytest.mark.parametrize("cycle_wait", [False, True])
def test_regular_interval_and_long_cycle_wait_do_not_raise_alarm(controller, cycle_wait):
    c = controller
    seed_measured_voltage(c)
    for t in (4, 10, 30, 60, 90):
        snapshot = show(
            c,
            t + 0.02,
            waiting("left", t, cycle_wait=cycle_wait),
            waiting("right", t, cycle_wait=cycle_wait),
            dict(reading("current", 100), started=t, finished=t + 0.01),
        )
        assert snapshot["alarms"] == []
        assert snapshot["cell_pair"] == []
        assert snapshot["metrics"]["quality"] == "partial"


def test_startup_wait_is_quiet_but_missing_first_state_eventually_alarms(controller):
    c = controller
    for t in (0, 0.3, 1, 2.9):
        assert c.snapshot_at(t)["alarms"] == []
    alarms = {a["key"] for a in c.snapshot_at(3.01)["alarms"]}
    assert {"controller_left", "controller_right"} <= alarms


def test_waiting_state_that_stops_updating_becomes_a_real_communication_alarm(controller):
    c = controller
    show(c, 0.02, waiting("left", 0), waiting("right", 0))
    assert c.snapshot_at(0.5)["alarms"] == []
    snapshot = c.snapshot_at(0.62)
    assert snapshot["controllers"]["left"]["status"] == "stale"
    assert {a["key"] for a in snapshot["alarms"]} == {"controller_left", "controller_right"}


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"error": "timeout"}, "read_error"),
        ({"parameters": [599, 0, 0, 15]}, "invalid_parameters"),
        ({"registers": [1, 350, 0, 0, 3, 0]}, "frozen"),
        ({"registers": [1, 100, 0, 0, 3, 0]}, "inconsistent"),
    ],
)
def test_genuine_faults_still_alarm_immediately(controller, change, expected):
    c = controller
    show(c, 2.52, packet("left", 2.5, 1, 3.5), waiting("right", 2.5))
    bad = dict(packet("left", 2.7, 1, 3.3), **change)
    snapshot = show(c, 2.72, bad)
    assert snapshot["controllers"]["left"]["status"] == expected
    assert any(a["key"] == "controller_left" for a in snapshot["alarms"])
    assert snapshot["controllers"]["left"]["active"] is False


def test_continuously_unconfirmed_channel_churn_is_not_silenced_forever():
    tracker = PulseTracker(profiles())
    for index in range(7):
        t = index * 0.2
        tracker.ingest(packet("left", t, 1 + index % 2, 6))
    state = tracker.snapshot(1.23)["left"]
    assert state["active"] is False
    assert state["status"] == "unconfirmed_timeout"
    assert state["alarm"] is True and state["healthy"] is False
