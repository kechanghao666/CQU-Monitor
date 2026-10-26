"""核心时序与统计验证；只使用虚拟单调时间，不接触串口或界面。"""

import importlib
import json
import math

import pytest


WALL = "2026-10-16T09:00:00+08:00"


def make_engine(config=None, synchronized=True):
    # 在测试函数中加载模块，使实现缺失时出现明确的行为测试失败。
    try:
        engine_type = importlib.import_module("cqu_monitor.engine").Engine
    except ModuleNotFoundError:
        pytest.fail("Engine 计算核心尚未实现")
    engine = engine_type(config or {}, "session-test")
    engine.start(100.0, WALL)
    if synchronized:
        engine.synchronize(100.0)
    return engine


def accept(engine, role, value, start, finish=None, error=None):
    return engine.accept(
        role,
        value,
        100.0 + start,
        100.0 + (start if finish is None else finish),
        WALL,
        error,
    )


def scan(engine, omit=None, value=2.0):
    for slot in range(15):
        for role, cell in (("voltage_left", slot + 1), ("voltage_right", slot + 16)):
            if cell != omit:
                accept(engine, role, value, slot * 6 + 2.1, slot * 6 + 2.2)


def test_scan_pairs_and_ninety_second_rollover():
    engine = make_engine()
    for moment, pair, round_number in ((2.1, [1, 16], 1), (86.1, [15, 30], 1), (92.1, [1, 16], 2)):
        snap = engine.snapshot(100 + moment)
        assert snap["cell_pair"] == pair
        assert snap["round"] == round_number
        sample = accept(engine, "voltage_left", 2, moment, moment + 0.05)
        assert sample["cell"] == pair[0]
        assert sample["round"] == round_number
        assert sample["valid"]


@pytest.mark.parametrize(
    "start,finish", [(1.99, 2.1), (3.9, 4), (3.9, 8.1), (2.1, 8.1), (5.9, 6.1)]
)
def test_voltage_requires_both_request_ends_inside_same_window(start, finish):
    engine = make_engine()
    sample = accept(engine, "voltage_left", 2.3, start, finish)
    assert not sample["valid"]
    assert sample["status"] == "out_of_window"
    assert sample["value"] == 2.3
    assert engine.snapshot(110)["cells"][0]["count"] == 0


def test_both_valid_readings_contribute_and_latest_is_second():
    engine = make_engine()
    first = accept(engine, "voltage_left", 2, 2.1, 2.2)
    second = accept(engine, "voltage_left", 4, 3.1, 3.2)
    assert first["valid"] and second["valid"]
    assert second["mean_value"] == 3
    cell = engine.snapshot(104)["cells"][0]
    assert (cell["latest"], cell["mean"], cell["count"]) == (4, 3, 2)


def test_twenty_nine_cells_form_partial_total_and_thirtieth_joins():
    engine = make_engine()
    scan(engine, omit=30)
    metrics = engine.snapshot(186.3)["metrics"]
    assert metrics["total_voltage"] == 58
    assert metrics["measured_count"] == 29 and metrics["quality"] == "partial"
    accept(engine, "voltage_right", 2, 86.3, 86.4)
    metrics = engine.snapshot(186.5)["metrics"]
    assert metrics["total_voltage"] == 60
    assert metrics["current"] is None
    assert metrics["power_kw"] is None


def test_total_keeps_latest_during_stale_or_failure_and_marks_quality():
    engine = make_engine()
    scan(engine)
    assert engine.snapshot(198.2)["metrics"]["total_voltage"] == 60
    metrics = engine.snapshot(198.2001)["metrics"]
    assert metrics["total_voltage"] == 60 and metrics["quality"] == "degraded"
    engine = make_engine()
    scan(engine)
    accept(engine, "voltage_left", None, 87, 87.1, "串口超时")
    metrics = engine.snapshot(187.2)["metrics"]
    assert metrics["total_voltage"] == 60 and metrics["available_count"] == 15
    # 窗口外成功读取恢复设备健康，但不会替换上次有效小室值。
    accept(engine, "voltage_left", 99, 88, 88.1)
    assert engine.snapshot(188.2)["metrics"]["total_voltage"] == 60


def test_resynchronize_clears_latest_but_keeps_cell_average():
    engine = make_engine()
    scan(engine)
    engine.synchronize(190)
    snap = engine.snapshot(190)
    assert snap["sync_version"] == 2
    assert snap["metrics"]["total_voltage"] is None
    assert snap["cells"][0]["latest"] is None
    assert snap["cells"][0]["mean"] == 2
    accept(engine, "voltage_left", 4, 92.1, 92.2)
    assert engine.snapshot(193)["cells"][0]["mean"] == 3


def test_current_is_continuous_and_expires_after_two_point_five_seconds():
    engine = make_engine()
    scan(engine)
    sample = accept(engine, "current", 100, 87, 87.1)
    assert sample["valid"] and sample["cell"] is None and sample["round"] is None
    assert engine.snapshot(189.6)["metrics"]["power_kw"] == 6
    metrics = engine.snapshot(189.6001)["metrics"]
    assert metrics["current"] is None and metrics["power_kw"] is None
    assert metrics["total_voltage"] == 60
    accept(engine, "current", None, 90, 90.1, "断开")
    assert engine.snapshot(190.2)["metrics"]["current"] is None


def test_means_use_unique_integer_tick_grid_and_snapshot_is_pure():
    engine = make_engine()
    scan(engine)
    accept(engine, "current", 100, 87, 87.1)
    first = engine.tick(187.2, WALL)
    assert first["avg_total_voltage"] == 60
    assert first["avg_power_kw"] == 6
    for _ in range(20):
        assert engine.snapshot(187.3)["metrics"]["avg_power_kw"] == 6
        assert engine.tick(187.8, WALL) is None
    accept(engine, "current", 200, 88, 88.1)
    second = engine.tick(188.2, WALL)
    assert second["avg_power_kw"] == 9
    # 跳过的秒不补造样本；此时电流已过期，只累计有效总压。
    third = engine.tick(194, WALL)
    assert third["power_kw"] is None and third["avg_power_kw"] == 9
    assert engine.tick(193, WALL) is None


@pytest.mark.parametrize(
    "value,error", [(None, None), (math.nan, None), (math.inf, None), (2.0, "协议错误")]
)
def test_invalid_values_and_errors_are_preserved_without_statistics(value, error):
    engine = make_engine()
    sample = accept(engine, "voltage_left", value, 2.1, 2.2, error)
    assert not sample["valid"]
    assert sample["error"] == error
    assert (
        sample["value"] is None
        if value is None or not math.isfinite(value)
        else sample["value"] == value
    )
    assert engine.snapshot(103)["cells"][0]["count"] == 0
    json.dumps(sample, allow_nan=False)
    json.dumps(engine.snapshot(103), allow_nan=False)


@pytest.mark.parametrize("start,finish", [(3.1, 2.1), (-1, 2.1), (math.nan, 2.1), (2.1, math.inf)])
def test_bad_timestamps_cannot_corrupt_existing_values(start, finish):
    engine = make_engine()
    accept(engine, "voltage_left", 2, 2.1, 2.2)
    sample = accept(engine, "voltage_left", 999, start, finish)
    assert not sample["valid"] and sample["status"] == "invalid_time"
    cell = engine.snapshot(104)["cells"][0]
    assert (cell["latest"], cell["count"]) == (2, 1)
    json.dumps(sample, allow_nan=False)


def test_out_of_order_response_does_not_overwrite_latest_or_role_health():
    engine = make_engine()
    accept(engine, "voltage_left", 4, 3.1, 3.2)
    old = accept(engine, "voltage_left", 2, 2.1, 2.2)
    assert not old["valid"] and old["status"] == "invalid_time"
    assert engine.snapshot(104)["cells"][0]["latest"] == 4


def test_zero_threshold_is_enabled_and_alarm_does_not_remove_reading():
    engine = make_engine({"voltage_high": 0, "current_low": 0})
    accept(engine, "voltage_left", 2, 2.1, 2.2)
    accept(engine, "current", -1, 3.2, 3.3)
    snap = engine.snapshot(103.5)
    assert snap["cells"][0]["latest"] == 2
    assert snap["metrics"]["current"] == -1
    assert len(snap["alarms"]) >= 2
    assert all(set(("key", "level", "message")) <= alarm.keys() for alarm in snap["alarms"])


def test_unsynchronized_voltage_rejected_but_current_accepted_and_start_resets():
    engine = make_engine(synchronized=False)
    sample = accept(engine, "voltage_left", 2, 2.1, 2.2)
    assert not sample["valid"] and sample["cell"] is None
    assert accept(engine, "current", 10, 2.1, 2.2)["valid"]
    engine.start(200, WALL)
    snap = engine.snapshot(200)
    assert not snap["synchronized"] and snap["sync_version"] == 0
    assert snap["metrics"]["current"] is None


@pytest.mark.parametrize(
    "config",
    [
        {"step_seconds": 0},
        {"step_seconds": 100},
        {"sample_interval": 0},
        {"valid_start": -1},
        {"valid_start": 4, "valid_end": 2},
        {"valid_end": 7},
        {"voltage_low": math.nan},
        {"voltage_low": 3, "voltage_high": 2},
    ],
)
def test_invalid_timing_and_threshold_configuration_is_rejected(config):
    with pytest.raises(ValueError):
        make_engine(config)


def test_invalid_clock_calls_raise_without_poisoning_state():
    engine = make_engine()
    for call in (
        lambda: engine.start(math.nan, WALL),
        lambda: engine.synchronize(99),
        lambda: engine.snapshot(math.inf),
        lambda: engine.tick(99, WALL),
    ):
        with pytest.raises(ValueError):
            call()
    assert engine.snapshot(103)["synchronized"]


def test_total_mean_counts_only_the_two_arrived_tick_seconds():
    engine = make_engine()
    scan(engine)
    assert engine.tick(187, WALL)["avg_total_voltage"] == 60
    accept(engine, "voltage_left", 4, 92.1, 92.2)
    accept(engine, "voltage_right", 4, 92.1, 92.2)
    for _ in range(10):
        assert engine.snapshot(192.3)["metrics"]["total_voltage"] == 64
    assert engine.tick(192.3, WALL)["avg_total_voltage"] == 62


def test_full_round_crossing_is_rejected_even_when_both_phases_are_valid():
    engine = make_engine()
    sample = accept(engine, "voltage_left", 2, 2.1, 92.2)
    assert not sample["valid"] and sample["status"] == "out_of_window"


def test_finite_readings_that_overflow_total_are_missing_and_still_json_safe():
    engine = make_engine()
    scan(engine, value=1.7e308)
    result = engine.tick(187, WALL)
    assert result["total_voltage"] is None and not result["complete"]
    assert result["reason"]
    json.dumps(result, allow_nan=False)
    json.dumps(engine.snapshot(187), allow_nan=False)


def test_new_current_failure_removes_previous_current_until_success():
    engine = make_engine()
    scan(engine)
    accept(engine, "current", 100, 86.3, 86.4)
    assert engine.snapshot(186.5)["metrics"]["power_kw"] == 6
    accept(engine, "current", None, 86.6, 86.7, "timeout")
    assert engine.snapshot(186.8)["metrics"]["power_kw"] is None
    accept(engine, "current", 200, 87.1, 87.2)
    assert engine.snapshot(187.3)["metrics"]["power_kw"] == 12


def test_snapshot_and_sample_and_summary_have_exact_contract_fields():
    engine = make_engine()
    snap = engine.snapshot(102)
    sample = accept(engine, "current", 1, 2.1, 2.2)
    summary = engine.tick(103, WALL)
    assert set(snap) == set(
        "session_id mode elapsed synchronized sync_version slot round phase cell_pair cells metrics alarms synchronization_source".split()
    )
    assert set(sample) == set(
        "session_id role cell round phase elapsed wall_time value valid status error mean_value sync_version synchronization_source sync_evidence".split()
    )
    assert set(summary) == set(
        "session_id elapsed wall_time total_voltage avg_total_voltage current power_kw avg_power_kw complete reason measured_count available_count coverage quality".split()
    )
    assert len(snap["cells"]) == 30
    assert set(snap["cells"][0]) == set("cell latest mean count updated_at age status".split())


def test_partial_sum_replaces_latest_and_all_cumulative_metrics_update():
    engine = make_engine()
    assert engine.snapshot(101)["metrics"]["total_voltage"] is None
    accept(engine, "voltage_left", 2, 2.1, 2.2)
    accept(engine, "current", 100, 2.1, 2.2)
    first = engine.tick(102.3, WALL)
    assert first["total_voltage"] == first["avg_total_voltage"] == 2
    assert first["power_kw"] == first["avg_power_kw"] == pytest.approx(0.2)
    assert first["measured_count"] == 1 and not first["complete"]
    accept(engine, "voltage_left", 4, 3.1, 3.2)
    accept(engine, "voltage_right", 3, 3.1, 3.2)
    second = engine.tick(103.3, WALL)
    assert second["total_voltage"] == 7
    assert second["avg_total_voltage"] == 4.5
    assert second["avg_power_kw"] == pytest.approx(0.45)
    assert second["measured_count"] == 2
