"""报警与实时显示专项审计；只注入测试数据包，绝不启动设备读取者。"""

import math
import json
from pathlib import Path
import sqlite3

import pytest

from cqu_monitor.config import default_settings
from cqu_monitor.controller import Controller
from cqu_monitor.engine import Engine
from cqu_monitor.ui import MainWindow

OUT = Path(__file__).resolve().parents[1] / "artifacts/alarm_live_125"
WALL = "2026-10-17T15:00:00+08:00"


class Clock:
    value = 0.0

    def now(self):
        return self.value


def settings(**changes):
    config = default_settings()
    config["synchronization"] = "manual"
    config.update(changes)
    return config


@pytest.mark.parametrize("cell", range(1, 31))
@pytest.mark.parametrize("bound,value", [("low", 0.8), ("high", 2.8)])
def test_every_cell_has_its_own_threshold_alarm(cell, bound, value):
    engine = Engine(settings(voltage_low=1.0, voltage_high=2.5), "audit")
    engine.start(0, WALL)
    engine.synchronize(0)
    t = ((cell - 1) % 15) * 6 + 2.1
    role = "voltage_left" if cell <= 15 else "voltage_right"
    engine.accept(role, value, t, t + 0.01, WALL)
    alarms = engine.snapshot(t + 0.02)["alarms"]
    assert {a["key"] for a in alarms} == {f"cell_{cell}_{bound}"}
    assert f"{cell}号小室" in alarms[0]["message"]


@pytest.mark.parametrize("role,low,high", [("voltage_left", 1.0, 2.5), ("current", 10, 200)])
def test_equal_limits_are_normal_and_disabled_limits_do_not_alarm(role, low, high):
    prefix = "current" if role == "current" else "voltage"
    config = settings(**{prefix + "_low": low, prefix + "_high": high})
    engine = Engine(config, "bounds")
    engine.start(0, WALL)
    engine.synchronize(0)
    for t, value in [(2.1, low), (3.1, high)]:
        engine.accept(role, value, t, t + 0.01, WALL)
        assert not engine.snapshot(t + 0.02)["alarms"]
    disabled = Engine(settings(), "disabled")
    disabled.start(0, WALL)
    disabled.synchronize(0)
    disabled.accept(role, high * 10, 2.1, 2.11, WALL)
    assert not disabled.snapshot(2.12)["alarms"]


@pytest.fixture
def app_controller(qtbot, tmp_path, monkeypatch):
    from cqu_monitor.alerts import AlarmSound

    monkeypatch.setattr(AlarmSound, "play", lambda self: None)
    monkeypatch.setattr(AlarmSound, "stop", lambda self: None)
    OUT.mkdir(parents=True, exist_ok=True)
    window = MainWindow(settings(current_high=110, voltage_high=2.3))
    window.setWindowTitle("报警与实时显示检查（测试信号，非实测）")
    qtbot.addWidget(window)
    c = Controller(window, window.settings, tmp_path, launch_workers=False)
    c.start()
    c.timer.stop()
    c.clock = Clock()
    c.engine.synchronize(0)
    yield c, window
    c.stop()
    assert not c.running and c.recorder.pending_count == 0


def feed(c, t, **values):
    for role, value in values.items():
        c.output.put(
            dict(
                kind="reading",
                role=role,
                value=value,
                started=t,
                finished=t + 0.01,
                error=None if value is not None else "专项测试断线",
            )
        )
    c.clock.value = t + 0.02
    c.poll()
    c.render()


def test_latest_mean_partial_total_power_and_charts_update_together(app_controller, qtbot):
    c, w = app_controller
    w.show()
    feed(c, 2.1, voltage_left=1.8, voltage_right=2.0, current=100)
    assert w.left_table.item(0, 1).text() == "1.800"
    assert w.right_table.item(0, 1).text() == "2.000"
    assert w.metric_values["total_voltage"].text() == "3.800"
    assert w.metric_values["power_kw"].text() == "0.380"
    assert w.left_table.item(1, 1).text() == "—"
    feed(c, 3.1, voltage_left=2.2, voltage_right=2.4, current=100)
    assert w.left_table.item(0, 1).text() == "2.200"
    assert w.left_table.item(0, 2).text() == "2.000"
    assert w.right_table.item(0, 2).text() == "2.200"
    assert w.metric_values["total_voltage"].text() == "4.600"
    assert w.metric_values["power_kw"].text() == "0.460"
    assert list(w.cell_charts[1].latest_values) == [1.8, 2.2]
    assert list(w.cell_charts[1].mean_values) == [1.8, 2.0]
    chart = w.cell_charts[1]
    chart.open_detail()
    assert chart.detail_dialog is not None
    feed(c, 3.5, voltage_left=2.0)
    assert chart.latest_values[-1] == 2.0
    assert w.left_table.item(0, 1).text() == "2.000"
    chart.detail_dialog.close()
    feed(c, 4.1, voltage_left=999, current=130)
    assert w.left_table.item(0, 1).text() == "2.000"
    assert w.cell_charts[1].latest_values[-1] == 2.0
    assert w.metric_values["current"].text() == "130.000"
    assert w.metric_values["total_voltage"].text() == "4.400"
    assert w.metric_values["power_kw"].text() == "0.572"
    assert "电流高于上限" in w.event_log.toPlainText()
    qtbot.wait(20)
    w.grab().save(str(OUT / "实时数值与超限日志_非实测.png"))


def test_repeated_alarm_not_spammed_and_true_recovery_is_persisted(app_controller):
    c, w = app_controller
    feed(c, 2.1, current=120)
    for _ in range(5):
        c.render()
    assert w.event_log.toPlainText().count("电流高于上限") == 1
    feed(c, 3.1, current=100)
    assert "报警恢复：current_high" in w.event_log.toPlainText()
    feed(c, 4.1, current=130)
    assert w.event_log.toPlainText().count("电流高于上限") == 2
    c.stop()
    with sqlite3.connect(c.store.db_path) as db:
        events = "\n".join(
            json.loads(row[0])["message"] for row in db.execute("SELECT payload FROM events")
        )
    assert "电流高于上限" in events and "报警恢复" in events


def test_current_failure_clears_live_current_power_and_inserts_curve_gap(app_controller):
    c, w = app_controller
    feed(c, 2.1, voltage_left=2, current=100)
    c.output.put(dict(kind="device", role="current", state="disconnected"))
    feed(c, 3.1, current=None)
    assert w.metric_values["current"].text() == "—"
    assert w.metric_values["power_kw"].text() == "—"
    assert w.metric_values["total_voltage"].text() == "2.000"
    assert math.isnan(w.summary_charts["current"].latest_values[-1])
    assert w.device_labels["current"].text() == "●  连接断开"
    assert "最近读取失败" in w.event_log.toPlainText()
    c.output.put(dict(kind="device", role="current", state="connected"))
    feed(c, 4.1, current=90)
    assert w.metric_values["current"].text() == "90.000"
    assert w.metric_values["power_kw"].text() == "0.180"
    assert w.device_labels["current"].text() == "●  已连接"


def test_current_stale_clears_display_and_warns(app_controller):
    c, w = app_controller
    feed(c, 2.1, current=100)
    c.clock.value = 5
    c.poll()
    c.render()
    assert w.metric_values["current"].text() == "—"
    assert "电流读数已过期" in w.event_log.toPlainText()


@pytest.mark.parametrize("failure", ["disconnected", "stale"])
def test_loss_of_current_must_not_be_reported_as_threshold_recovery(app_controller, failure):
    """未知电流不能被描述为超限恢复；该断言用于发现潜在的误导提示。"""
    c, w = app_controller
    feed(c, 2.1, current=130)
    if failure == "disconnected":
        feed(c, 3.1, current=None)
    else:
        c.clock.value = 5
        c.poll()
        c.render()
    assert "报警恢复：current_high" not in w.event_log.toPlainText()


def test_popup_remains_open_while_live_readings_update(app_controller, qtbot):
    c, w = app_controller
    w.show()
    feed(c, 2.1, voltage_left=2, current=130)
    notice = w.alarm_notifier
    assert notice.dialog.isVisible() and not notice.dialog.isModal()
    first = w.metric_values["current"].text()
    feed(c, 3.1, voltage_left=2.2, current=140)
    assert w.metric_values["current"].text() != first
    assert w.metric_values["power_kw"].text() == "0.308"
    assert notice.dialog.isVisible()
    qtbot.wait(20)
    w.grab().save(str(OUT / "实时数据与报警入口_非实测.png"))
    notice.dialog.grab().save(str(OUT / "报警弹窗_非实测.png"))
    notice.acknowledge()
    feed(c, 4.1, current=150)
    assert not notice.dialog.isVisible()
    feed(c, 5.1, current=100)
    feed(c, 6.1, current=140)
    assert notice.dialog.isVisible()
    c.stop()
    assert not notice.timer.isActive()


@pytest.mark.parametrize("role", ["voltage_left", "voltage_right", "current"])
def test_each_meter_disconnect_opens_alarm(app_controller, role):
    c, w = app_controller
    w.show()
    feed(c, 2.1, voltage_left=2, voltage_right=2, current=100)
    c.output.put(dict(kind="device", role=role, state="disconnected"))
    feed(c, 3.1, **{role: None})
    assert f"{role}_offline" in w.alarm_notifier.active
    assert w.alarm_notifier.dialog.isVisible()
    assert w.device_labels[role].text() == "●  连接断开"


@pytest.mark.parametrize("group", ["left", "right"])
def test_each_controller_disconnect_opens_alarm(qtbot, tmp_path, monkeypatch, group):
    from cqu_monitor.alerts import AlarmSound

    monkeypatch.setattr(AlarmSound, "play", lambda self: None)
    monkeypatch.setattr(AlarmSound, "stop", lambda self: None)
    w = MainWindow(default_settings())
    qtbot.addWidget(w)
    c = Controller(w, w.settings, tmp_path, launch_workers=False)
    c.start()
    c.timer.stop()
    c.clock = Clock()
    w.show()
    try:
        role = f"controller_{group}"
        c.output.put(dict(kind="device", role=role, state="disconnected"))
        c.output.put(
            dict(
                kind="controller",
                group=group,
                started=2.1,
                finished=2.11,
                parameters=None,
                registers=None,
                error="专项测试控制器断线",
            )
        )
        c.clock.value = 2.12
        c.poll()
        c.render()
        assert role in w.alarm_notifier.active
        assert w.alarm_notifier.dialog.isVisible()
        assert w.device_labels[role].text() == "●  连接断开"
    finally:
        c.stop()
