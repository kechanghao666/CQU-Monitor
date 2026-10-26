"""五路连接状态与异步停止阶段的界面回归，不连接实际串口。"""

import threading

import pytest
from PySide6.QtWidgets import QLabel

from cqu_monitor.config import default_settings
from cqu_monitor.controller import Controller
from cqu_monitor.ui import MainWindow

ROLES = ("voltage_left", "voltage_right", "current", "controller_left", "controller_right")


def test_five_port_cards_show_configured_roles_and_ports(qtbot):
    settings = default_settings()
    settings["controllers"]["right"]["port"] = "COM9"
    window = MainWindow(settings)
    qtbot.addWidget(window)
    assert tuple(window.device_names) == ROLES
    assert [label.text() for label in window.device_names.values()] == [
        "COM1（电压A）",
        "COM2（电压B）",
        "COM3（电流）",
        "COM4（控制器A）",
        "COM9（控制器B）",
    ]
    window.settings["controllers"]["left"]["port"] = "COM8"
    window.set_devices(dict.fromkeys(ROLES, "connected"))
    assert window.device_names["controller_left"].text() == "COM8（控制器A）"


@pytest.mark.parametrize(
    "state,caption,color",
    [
        ("connected", "已连接", "#078575"),
        ("connecting", "连接中", "#4b82ae"),
        ("disconnected", "连接断开", "#bd5555"),
    ],
)
def test_all_five_cards_share_the_same_state_words_and_colors(qtbot, state, caption, color):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    window.set_devices(dict.fromkeys(ROLES, state))
    assert len(window.device_labels) == 5
    assert {label.text() for label in window.device_labels.values()} == {"●  " + caption}
    assert all(color in label.styleSheet() for label in window.device_labels.values())


def test_fault_banner_removed_without_losing_alarm_events(qtbot, tmp_path):
    settings = default_settings()
    settings["synchronization"] = "manual"
    settings["current_high"] = 10
    window = MainWindow(settings)
    qtbot.addWidget(window)
    c = Controller(window, settings, tmp_path, launch_workers=False)
    try:
        c.start()
        c.output.put(
            dict(kind="reading", role="current", value=100, started=0.1, finished=0.2, error=None)
        )
        c.drain()
        c.render()
        assert not hasattr(window, "alarm_label")
        assert not any(
            "阈值报警需在参数中手动启用" in label.text() for label in window.findChildren(QLabel)
        )
        assert (
            "电流" in window.event_log.toPlainText()
            and "高于上限" in window.event_log.toPlainText()
        )
    finally:
        c.stop()


def test_readers_exit_before_saving_caption_and_all_five_reset_together(
    qtbot, tmp_path, monkeypatch
):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    c = Controller(window, default_settings(), tmp_path, launch_workers=False)
    c.start()
    released, saving, allow_save = threading.Event(), threading.Event(), threading.Event()

    class Reader:
        pending = []
        failure = None

        def is_alive(self):
            return not released.is_set()

    c.workers = [Reader()]
    finish = c.store.finish_session

    def slow_finish(*args, **kwargs):
        saving.set()
        allow_save.wait(3)
        return finish(*args, **kwargs)

    monkeypatch.setattr(c.store, "finish_session", slow_finish)
    try:
        window.stop_button.click()
        assert window.run_label.text() == "●  正在停止读取…"
        assert {label.text() for label in window.device_labels.values()} == {"●  正在停止读取…"}
        window.set_devices(dict.fromkeys(ROLES, "disconnected"))
        assert {label.text() for label in window.device_labels.values()} == {"●  正在停止读取…"}
        released.set()
        qtbot.waitUntil(saving.is_set, timeout=3000)
        assert window.run_label.text() == "●  正在保存数据…"
        assert {label.text() for label in window.device_labels.values()} == {"●  正在保存数据…"}
        assert not window.start_button.isEnabled()
    finally:
        released.set()
        allow_save.set()
        qtbot.waitUntil(lambda: not c.running, timeout=4000)
    assert window.run_label.text() == "●  待机"
    assert {label.text() for label in window.device_labels.values()} == {"●  待机"}
    assert c.recorder.pending_count == 0


def test_manual_mode_keeps_both_controller_cards_visible_and_disabled(qtbot):
    settings = default_settings()
    settings["synchronization"] = "manual"
    window = MainWindow(settings)
    qtbot.addWidget(window)
    window.set_devices(dict.fromkeys(ROLES, "connected"))
    assert window.device_labels["controller_left"].text() == "●  未启用"
    assert window.device_labels["controller_right"].text() == "●  未启用"
    assert not window.device_labels["controller_left"].isHidden()
