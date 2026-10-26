import json

import pytest

from cqu_monitor.pulse import PulseTracker


def profiles():
    return {
        group: {"port": f"COM{4 + index}", "unit": 1, "countdown_scale": 0.01}
        for index, group in enumerate(("left", "right"))
    }


def packet(t, remaining, *, group="left", channel=1, cycle=1, **changes):
    result = {
        "kind": "controller",
        "group": group,
        "started": t,
        "finished": t + 0.02,
        "parameters": [600, 0, 0, 15],
        "registers": [channel, remaining, 0, 0, cycle, 0],
        "error": None,
    }
    result.update(changes)
    return result


def bracket(tracker, *, group="left", channel=1):
    tracker.ingest(packet(1.8, 420, group=group, channel=channel))
    tracker.ingest(packet(3.3, 270, group=group, channel=channel))
    return tracker.assess(group, 2.2, 3.0, 3.35)


def test_valid_bracket_keeps_auditable_sequences_and_raw_observations():
    tracker = PulseTracker(profiles())
    context = bracket(tracker)
    assert context["valid"] is True
    assert context["cell"] == 1
    assert context["round"] == 1
    assert 2 <= context["phase_min"] < context["phase_max"] < 4
    assert context["source"] == "controller"
    assert context["sample_started"] == 2.2
    assert context["sample_finished"] == 3.0
    assert context["before"]["sequence"] == 1
    assert context["after"]["sequence"] == 2
    assert "registers" not in context["before"]
    observation = tracker.history["left"][-1]
    assert observation["raw_parameters"] == [600, 0, 0, 15]
    assert observation["raw_registers"][1] == 270
    json.dumps(context, allow_nan=False)
    json.dumps(observation, allow_nan=False)


def test_groups_are_independent_and_right_cell_is_offset():
    tracker = PulseTracker(profiles())
    assert bracket(tracker, group="right", channel=4)["cell"] == 19
    assert tracker.snapshot(3.35)["left"]["active"] is False
    assert tracker.snapshot(3.35)["right"]["cell"] == 19


def test_snapshot_needs_two_fresh_decreasing_consistent_observations():
    tracker = PulseTracker(profiles())
    tracker.ingest(packet(2.5, 350))
    assert tracker.snapshot(2.53)["left"]["active"] is False
    tracker.ingest(packet(2.7, 330))
    assert tracker.snapshot(2.73)["left"]["active"] is True
    assert tracker.snapshot(3.34)["left"]["active"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"parameters": [599, 0, 0, 15]},
        {"parameters": [600, 0, 0, 14]},
        {"parameters": [600, 0, 0]},
        {"registers": [1, 330, 0, 0, 1]},
        {"registers": [1, 601, 0, 0, 1, 0]},
        {"registers": [16, 330, 0, 0, 1, 0]},
        {"registers": [1, True, 0, 0, 1, 0]},
        {"registers": [1, 330, 0, 1, 1, 0]},
        {"registers": [1, 330, 0, 0, 1, 1]},
        {"registers": [1, 0, 0, 0, 1, 0]},
        {"error": "timeout"},
        {"started": float("nan")},
        {"finished": 1.0},
    ],
)
def test_invalid_observation_cancels_prior_active_state(change):
    tracker = PulseTracker(profiles())
    tracker.ingest(packet(2.5, 350))
    tracker.ingest(packet(2.7, 330))
    assert tracker.snapshot(2.73)["left"]["active"]
    observation = tracker.ingest(packet(2.9, 310, **change))
    assert tracker.snapshot(2.95)["left"]["active"] is False
    json.dumps(observation, allow_nan=False)


@pytest.mark.parametrize("remaining", [350, 351, 100])
def test_frozen_reversed_or_inconsistent_countdown_fails_closed(remaining):
    tracker = PulseTracker(profiles())
    tracker.ingest(packet(2.5, 350))
    tracker.ingest(packet(2.7, remaining))
    assert tracker.snapshot(2.73)["left"]["active"] is False


@pytest.mark.parametrize("started,finished", [(1.99, 2.1), (3.8, 4.0)])
def test_whole_sample_must_fit_conservative_window(started, finished):
    tracker = PulseTracker(profiles())
    tracker.ingest(packet(started - 0.1, round((6 - started + 0.1) * 100)))
    tracker.ingest(packet(finished + 0.1, round((6 - finished - 0.1) * 100)))
    context = tracker.assess("left", started, finished, finished + 0.13)
    assert context["valid"] is False
    assert context["cell"] == 1


def test_wait_for_after_is_bounded_and_final_does_not_wait():
    tracker = PulseTracker(profiles())
    tracker.ingest(packet(2.0, 400))
    assert tracker.assess("left", 2.2, 2.3, 2.5) is None
    assert tracker.assess("left", 2.2, 2.3, 3.31)["valid"] is False
    assert tracker.assess("left", 2.2, 2.3, 2.5, final=True)["valid"] is False


@pytest.mark.parametrize(
    "middle",
    [
        packet(2.6, 340, channel=2),
        packet(2.6, 340, cycle=2),
        packet(2.6, 340, error="timeout"),
        packet(2.6, 500),
        packet(2.6, 420),
    ],
)
def test_transition_error_reset_or_freeze_between_brackets_rejects(middle):
    tracker = PulseTracker(profiles())
    tracker.ingest(packet(1.8, 420))
    tracker.ingest(middle)
    tracker.ingest(packet(3.3, 270))
    assert tracker.assess("left", 2.2, 3.0, 3.35)["valid"] is False


@pytest.mark.parametrize(
    "before,after",
    [
        (packet(1.0, 500), packet(3.3, 270)),
        (packet(1.8, 420), packet(3.7, 230)),
        (packet(1.8, 420, finished=2.01), packet(3.3, 270)),
        (packet(1.8, 420), packet(3.3, 420)),
    ],
)
def test_stale_slow_or_frozen_brackets_reject(before, after):
    tracker = PulseTracker(profiles())
    tracker.ingest(before)
    tracker.ingest(after)
    assert tracker.assess("left", 2.2, 3.0, 3.75)["valid"] is False


def test_history_is_bounded_and_time_must_be_ordered():
    tracker = PulseTracker(profiles())
    for index in range(180):
        tracker.ingest(packet(index * 0.2, 300))
    assert len(tracker.history["left"]) == 150
    assert tracker.history["left"][-1]["sequence"] == 180
    tracker.ingest(packet(1, 200))
    assert tracker.snapshot(36)["left"]["active"] is False


def test_profile_countdown_scale_is_applied():
    settings = profiles()
    settings["left"]["countdown_scale"] = 0.1
    tracker = PulseTracker(settings)
    tracker.ingest(packet(2.5, 35))
    tracker.ingest(packet(2.7, 33))
    assert tracker.snapshot(2.73)["left"]["active"] is True
    assert tracker.history["left"][-1]["remaining"] == pytest.approx(3.3)


def test_future_after_observation_cannot_confirm_present_sample():
    tracker = PulseTracker(profiles())
    tracker.ingest(packet(1.8, 420))
    tracker.ingest(packet(3.3, 270))
    assert tracker.assess("left", 2.2, 3.0, 3.1)["valid"] is False


def test_coincident_zero_duration_observation_waits_for_distinct_after():
    tracker = PulseTracker(profiles())
    tracker.ingest(packet(2.5, 350, finished=2.5))
    assert tracker.assess("left", 2.5, 2.5, 2.5) is None
    tracker.ingest(packet(2.7, 330, finished=2.71))
    context = tracker.assess("left", 2.5, 2.5, 2.72)
    assert context["valid"] is True
    assert context["before"]["sequence"] == 1
    assert context["after"]["sequence"] == 2
    assert 2 <= context["phase_min"] <= context["phase_max"] < 4


@pytest.mark.parametrize("now,final", [(3.5, False), (2.5, True)])
def test_same_observation_cannot_be_reused_as_after_at_deadline_or_final(now, final):
    tracker = PulseTracker(profiles())
    tracker.ingest(packet(2.5, 350, finished=2.5))
    context = tracker.assess("left", 2.5, 2.5, now, final=final)
    assert context["valid"] is False
    assert context["after"] is None


@pytest.mark.parametrize("t,remaining", [(2.0, 400), (4.0, 200)])
def test_coincident_observation_still_respects_conservative_window(t, remaining):
    tracker = PulseTracker(profiles())
    tracker.ingest(packet(t, remaining, finished=t))
    tracker.ingest(packet(t + 0.2, remaining - 20, finished=t + 0.21))
    context = tracker.assess("left", t, t, t + 0.22)
    assert context["valid"] is False
    assert context["status"] == "outside_window"
    assert context["before"]["sequence"] < context["after"]["sequence"]
