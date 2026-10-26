"""COM标签与表格填满可用区域的回归验证。"""

import pytest
from PySide6.QtWidgets import QLabel

from cqu_monitor.config import default_settings
from cqu_monitor.dialogs import SettingsDialog
from cqu_monitor.ui import MainWindow


def test_default_ports_and_both_sets_of_device_names(qtbot):
    settings = default_settings()
    roles = ("voltage_left", "voltage_right", "current")
    names = ("COM1（电压A）", "COM2（电压B）", "COM3（电流）")
    assert [settings["ports"][role]["port"] for role in roles] == ["COM1", "COM2", "COM3"]
    window = MainWindow(settings)
    dialog = SettingsDialog(settings)
    qtbot.addWidget(window)
    qtbot.addWidget(dialog)
    for role, name in zip(roles, names):
        assert window.device_names[role].text() == name
        assert dialog.device_names[role].text() == name
    dialog.port_controls["voltage_left"]["port"].setCurrentText("COM8")
    assert dialog.device_names["voltage_left"].text() == "COM8（电压A）"
    window.settings["ports"]["voltage_left"]["port"] = "COM8"
    window.set_devices(dict(voltage_left="connected"))
    assert window.device_names["voltage_left"].text() == "COM8（电压A）"


@pytest.mark.parametrize("size", [(1440, 1000), (1600, 1100)])
def test_no_empty_viewport_below_fifteenth_row(qtbot, size):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    window.resize(*size)
    window.show()
    qtbot.wait(30)
    for table in (window.left_table, window.right_table):
        rect = table.visualItemRect(table.item(14, 0))
        assert abs(table.viewport().height() - 1 - rect.bottom()) <= 2
        assert rect.bottom() < table.viewport().height()


def test_formal_device_names_contain_no_duplicate_old_captions(qtbot):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    labels = [label.text() for label in window.findChildren(QLabel)]
    assert "电压采集 A" not in labels and "电压采集 B" not in labels and "电流采集" not in labels
