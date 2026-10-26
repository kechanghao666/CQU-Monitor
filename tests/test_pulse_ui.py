"""控制器同步界面契约；测试数据只驻留测试，均不打开串口。"""

from copy import deepcopy

import pytest

from cqu_monitor.config import default_settings
from cqu_monitor.dialogs import SettingsDialog
from cqu_monitor.ui import MainWindow


def controller_settings():
    settings = default_settings()
    settings["synchronization"] = "controller"
    settings["controllers"] = {
        group: dict(
            port=port,
            baudrate=9600,
            unit=1,
            parity="E",
            stopbits=1,
            timeout=0.1,
            function=3,
            address_offset=0,
            countdown_scale=0.01,
        )
        for group, port in [("left", "COM4"), ("right", "COM5")]
    }
    return settings


def test_controller_settings_preserve_meters_and_roundtrip(qtbot):
    settings = controller_settings()
    original = deepcopy(settings)
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)
    assert dialog.sync_combo.currentData() == "controller"
    assert set(dialog.port_controls) == {"voltage_left", "voltage_right", "current"}
    assert set(dialog.device_names) == set(dialog.port_controls)
    assert dialog.values()["controllers"] == settings["controllers"]
    controls = dialog.controller_controls["right"]
    controls["port"].setCurrentText("COM8")
    controls["parity"].setCurrentIndex(controls["parity"].findData("N"))
    controls["stopbits"].setCurrentIndex(controls["stopbits"].findData(2))
    controls["function"].setCurrentIndex(controls["function"].findData(4))
    controls["address_offset"].setCurrentIndex(controls["address_offset"].findData(-1))
    controls["countdown_scale"].setCurrentIndex(controls["countdown_scale"].findData(0.1))
    result = dialog.values()
    assert result["ports"] == original["ports"]
    assert result["controllers"]["right"] == {
        **original["controllers"]["right"],
        "port": "COM8",
        "parity": "N",
        "stopbits": 2,
        "function": 4,
        "address_offset": -1,
        "countdown_scale": 0.1,
    }
    assert settings == original
    dialog.show()
    qtbot.wait(20)
    assert dialog.height() <= 700


def test_countdown_precision_options_only_offer_supported_units(qtbot):
    dialog = SettingsDialog(controller_settings())
    qtbot.addWidget(dialog)
    for controls in dialog.controller_controls.values():
        combo = controls["countdown_scale"]
        assert [combo.itemData(i) for i in range(combo.count())] == [0.01, 0.1]


def test_invalid_existing_countdown_precision_cannot_be_silently_replaced(qtbot):
    settings = controller_settings()
    settings["controllers"]["left"]["countdown_scale"] = 1
    original = deepcopy(settings)
    with pytest.raises(ValueError, match="倒计时.*有效窗口"):
        SettingsDialog(settings)
    assert settings == original


def test_refresh_includes_both_controllers_without_connecting(qtbot, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "cqu_monitor.dialogs.list_ports.comports",
        lambda: [SimpleNamespace(device="COM12", description="USB")],
    )
    dialog = SettingsDialog(controller_settings())
    qtbot.addWidget(dialog)
    for controls in (*dialog.port_controls.values(), *dialog.controller_controls.values()):
        assert controls["port"].findText("COM12") >= 0
    assert dialog.controller_controls["left"]["port"].currentText() == "COM4"


def test_legacy_settings_select_manual_explicitly(qtbot):
    settings = controller_settings()
    settings.pop("synchronization")
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)
    assert dialog.sync_combo.currentData() == "manual"
    assert dialog.values()["synchronization"] == "manual"


def test_controller_accept_validates_shared_bus_and_meter_conflicts(qtbot):
    dialog = SettingsDialog(controller_settings())
    qtbot.addWidget(dialog)
    left, right = (dialog.controller_controls[group] for group in ("left", "right"))
    right["port"].setCurrentText("COM4")
    dialog.accept()
    assert dialog.result() == 0 and dialog.error_label.text()
    right["unit"].setValue(2)
    right["parity"].setCurrentIndex(right["parity"].findData("N"))
    dialog.accept()
    assert dialog.result() == 0 and dialog.error_label.text()
    right["port"].setCurrentText("COM1")
    dialog.accept()
    assert dialog.result() == 0 and dialog.error_label.text()
    right["port"].setCurrentText("COM5")
    dialog.accept()
    assert dialog.result() == 1


def state(cell, channel, active=True, phase="output", **extra):
    return dict(
        cell=cell,
        channel=channel,
        phase=phase,
        active=active,
        status="ok",
        port="COM4" if cell and cell <= 15 else "COM5",
        unit=1,
        **extra,
    )


def test_independent_groups_progress_no_false_pair_and_reset(qtbot):
    window = MainWindow(controller_settings())
    qtbot.addWidget(window)
    window.set_running(True)
    assert not window.sync_button.isEnabled()
    assert "控制器" in window.sync_button.text()
    assert "6秒" in window.footer_label.text()
    window.render(
        dict(
            synchronization_source="controller",
            cell_pair=[3],
            controllers={
                "left": state(3, 3),
                "right": state(23, 8, False, "interval", reason="间隔等待"),
            },
            alarms=[dict(message="旧报警")],
        )
    )
    assert "当前小室 03" in window.controller_labels["left"].text()
    assert "间隔" in window.controller_labels["right"].text()
    assert "当前小室 23" not in window.controller_labels["right"].text()
    assert "11/30" in window.scan_label.text()
    assert "A：3" in window.scan_label.toolTip()
    assert "B：未接通" in window.scan_label.toolTip()
    assert "有效样本数" in window.scan_label.toolTip()
    assert window.left_table.item(2, 0).background().color().name() == "#dcf4f0"
    assert window.right_table.item(7, 0).background().color().name() != "#dcf4f0"
    window.render(
        dict(
            synchronization_source="controller",
            controllers={
                "left": state(None, None, False, "unknown", reason="连接断开"),
                "right": state(24, 9),
            },
        )
    )
    assert "12/30" in window.scan_label.text()
    assert "连接断开" in window.controller_labels["left"].text()
    window.reset_session("hardware")
    assert "0/30" in window.scan_label.text()
    assert "当前小室" not in window.controller_labels["right"].text()
    assert not hasattr(window, "alarm_label")
    assert "等待" in window.controller_labels["left"].text()


def test_manual_mode_button_and_legacy_progress_remain(qtbot):
    settings = controller_settings()
    settings["synchronization"] = "manual"
    window = MainWindow(settings)
    qtbot.addWidget(window)
    window.set_running(True, waiting_sync=True)
    assert window.sync_button.isEnabled() and window.sync_button.text() == "手动同步"
    window.render(dict(synchronized=True, slot=4, round=1, phase=2.1, cell_pair=[5, 20]))
    assert "10/30" in window.scan_label.text()


def test_numeric_controller_phase_status_and_two_active_groups(qtbot):
    window = MainWindow(controller_settings())
    qtbot.addWidget(window)
    window.render(
        dict(
            synchronization_source="controller",
            cell_pair=[2, 24],
            controllers={
                "left": state(2, 2, phase=2.35),
                "right": {**state(24, 9, phase=3.1), "status": "valid"},
            },
        )
    )
    assert "输出 2.4s" in window.controller_labels["left"].text()
    assert "输出 3.1s" in window.controller_labels["right"].text()
    assert "11/30" in window.scan_label.text()
    assert "A：2；B：24" in window.scan_label.toolTip()
    window.render(
        dict(
            synchronization_source="controller",
            controllers={
                "left": {
                    **state(None, None, False, None),
                    "status": "frozen",
                    "reason": "倒计时未递减",
                },
                "right": {
                    **state(None, None, False, None),
                    "status": "read_error",
                    "reason": "读取失败",
                },
            },
        )
    )
    assert "倒计时停滞" in window.controller_labels["left"].text()
    assert "读取异常" in window.controller_labels["right"].text()
    assert not hasattr(window, "alarm_label")
