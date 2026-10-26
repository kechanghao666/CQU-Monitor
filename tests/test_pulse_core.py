from copy import deepcopy
import json

import pytest

from cqu_monitor.config import default_settings, load_settings, validate_settings
from cqu_monitor.engine import Engine


def config():
    value = default_settings()
    value["synchronization"] = "controller"
    return value


def context(cell=5, start=2.5, finish=2.51):
    return dict(
        valid=True,
        status="valid",
        source="controller",
        cell=cell,
        round=3,
        phase=2.5,
        phase_min=2.49,
        phase_max=2.53,
        sample_started=start,
        sample_finished=finish,
        before={"sequence": 3},
        after={"sequence": 4},
    )


def test_default_controller_profiles_and_shared_bus_constraints():
    settings = default_settings()
    assert settings["synchronization"] == "controller"
    assert settings["controllers"]["left"]["port"] == "COM4"
    assert settings["controllers"]["right"]["port"] == "COM5"
    validate_settings(settings, for_start=True)
    settings["controllers"]["right"]["port"] = "COM4"
    with pytest.raises(ValueError, match="站号"):
        validate_settings(settings, for_start=True)
    settings["controllers"]["right"]["unit"] = 2
    validate_settings(settings, for_start=True)
    settings["controllers"]["right"]["parity"] = "O"
    with pytest.raises(ValueError, match="参数"):
        validate_settings(settings, for_start=True)


def test_controller_cannot_share_meter_port_and_invalid_options_rejected():
    settings = config()
    settings["controllers"]["left"]["port"] = "COM1"
    with pytest.raises(ValueError, match="仪表"):
        validate_settings(settings, for_start=True)
    for key, value in [
        ("function", 6),
        ("address_offset", -2),
        ("countdown_scale", 0),
        ("parity", "bad"),
        ("stopbits", 3),
        ("unit", 100),
    ]:
        changed = config()
        changed["controllers"]["left"][key] = value
        with pytest.raises(ValueError):
            validate_settings(changed, for_start=True)


def test_profile_merge_for_existing_configuration(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "ports": default_settings()["ports"],
                "controllers": {"left": {"port": "COM8", "unit": 3}},
            }
        ),
        encoding="utf-8",
    )
    result = load_settings(path)
    assert result["synchronization"] == "controller"
    assert result["controllers"]["left"]["port"] == "COM8"
    assert result["controllers"]["left"]["countdown_scale"] == 0.01
    assert result["controllers"]["right"]["port"] == "COM5"


@pytest.mark.parametrize("group", ["left", "right"])
@pytest.mark.parametrize("for_start", [False, True])
@pytest.mark.parametrize("scale", [1, 0.5, 0.001, None, True])
def test_unsupported_countdown_precision_is_rejected_without_changing_settings(
    group, for_start, scale
):
    settings = config()
    settings["controllers"][group]["countdown_scale"] = scale
    original = deepcopy(settings)
    with pytest.raises(ValueError, match="倒计时.*0.01.*0.1.*有效窗口"):
        validate_settings(settings, for_start=for_start)
    assert settings == original


@pytest.mark.parametrize("scale", [0.01, 0.1])
def test_supported_countdown_precision_is_accepted(scale):
    settings = config()
    for profile in settings["controllers"].values():
        profile["countdown_scale"] = scale
    assert validate_settings(settings, for_start=True) is settings


@pytest.mark.parametrize("source", ["controller", "manual"])
def test_existing_one_second_countdown_configuration_is_not_silently_migrated(tmp_path, source):
    settings = config()
    settings["synchronization"] = source
    settings["controllers"]["left"]["countdown_scale"] = 1
    path = tmp_path / "settings.json"
    text = json.dumps(settings, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="配置文件无法读取.*倒计时.*有效窗口"):
        load_settings(path)
    assert path.read_text(encoding="utf-8") == text


def test_controller_engine_accepts_independent_group_context_and_partial_total():
    e = Engine(config(), "pulse-session")
    e.start(0, "2026-10-16T12:00:00+08:00")
    e.set_controller_health("left", True)
    e.set_controller_health("right", True)
    left = e.accept("voltage_left", 1.8, 2.5, 2.51, "t", sync_context=context(5))
    right = e.accept("voltage_right", 1.9, 2.5, 2.51, "t", sync_context=context(27))
    e.accept("current", 100, 2.5, 2.51, "t")
    assert left["valid"] and left["cell"] == 5
    assert right["valid"] and right["cell"] == 27
    assert left["sync_evidence"]["before"]["sequence"] == 3
    assert left["synchronization_source"] == "controller"
    snap = e.snapshot(2.6)
    assert snap["metrics"]["total_voltage"] == pytest.approx(3.7)
    assert snap["metrics"]["power_kw"] == pytest.approx(0.37)
    e.set_controller_health("left", False)
    snap = e.snapshot(2.7)
    assert snap["metrics"]["total_voltage"] == pytest.approx(3.7)
    assert snap["cells"][4]["status"] == "error"
    assert snap["metrics"]["quality"] == "degraded"


def test_controller_mode_never_falls_back_to_manual_time():
    e = Engine(config(), "s")
    e.start(0, "wall")
    with pytest.raises(ValueError, match="控制器"):
        e.synchronize(0)
    sample = e.accept("voltage_left", 2, 2.5, 2.51, "t")
    assert not sample["valid"] and sample["cell"] is None
    assert e.accept("current", 10, 2.5, 2.51, "t")["valid"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(cell=20),
        lambda c: c.update(phase_min=1.99),
        lambda c: c.update(phase_max=4),
        lambda c: c.update(sample_started=2.4),
        lambda c: c.update(valid=False, status="transition"),
    ],
)
def test_bad_controller_evidence_cannot_enter_means(mutate):
    e = Engine(config(), "s")
    e.start(0, "wall")
    evidence = deepcopy(context())
    mutate(evidence)
    sample = e.accept("voltage_left", 2, 2.5, 2.51, "t", sync_context=evidence)
    assert not sample["valid"]
    assert e.snapshot(2.6)["cells"][4]["count"] == 0


def test_sync_timeout_must_not_starve_healthy_shared_controller():
    value = config()
    for profile in value["controllers"].values():
        profile.update(port="COM4", timeout=2)
    value["controllers"]["right"]["unit"] = 2
    with pytest.raises(ValueError, match="0.2"):
        validate_settings(value, for_start=True)
