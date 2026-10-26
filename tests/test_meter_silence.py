"""驱动阻塞且没有返回失败包时，也应及时告警；不创建实际串口。"""

import pytest

from cqu_monitor.config import default_settings
from cqu_monitor.controller import Controller
from cqu_monitor.ui import MainWindow


class Clock:
    value = 0.0

    def now(self):
        return self.value


@pytest.fixture
def monitoring(qtbot, tmp_path):
    settings = default_settings()
    settings["synchronization"] = "manual"
    window = MainWindow(settings)
    qtbot.addWidget(window)
    controller = Controller(window, settings, tmp_path, launch_workers=False)
    controller.start()
    controller.timer.stop()
    controller.clock = Clock()
    controller.engine.synchronize(0)
    # 只启用生产监测分支；start已完成，不会启动读取者或打开串口。
    controller.launch_workers = True
    yield controller, window
    controller.stop()


def reading(controller, role, value, at):
    controller._consume_packet(
        dict(kind="reading", role=role, value=value, started=at, finished=at + 0.01)
    )
    controller._consume_packet(dict(kind="device", role=role, state="connected"))


@pytest.mark.parametrize("role", ["voltage_left", "voltage_right", "current"])
def test_first_request_hang_has_finite_grace_and_disconnect_alarm(monitoring, role):
    controller, window = monitoring
    assert not controller.snapshot_at(3)["alarms"]
    snapshot = controller.snapshot_at(3.01)
    alert = next(a for a in snapshot["alarms"] if a["key"] == f"{role}_offline")
    assert "未返回" in alert["message"]
    assert window.device_labels[role].text() == "●  连接断开"
    assert snapshot["metrics"]["current"] is None


@pytest.mark.parametrize("role", ["voltage_left", "voltage_right", "current"])
def test_silence_after_success_marks_quality_and_recovers_on_real_response(monitoring, role):
    controller, window = monitoring
    for meter, value in [("voltage_left", 2), ("voltage_right", 2.1), ("current", 100)]:
        reading(controller, meter, value, 2.1)
    snapshot = controller.snapshot_at(4.62)
    assert any(a["key"] == f"{role}_offline" for a in snapshot["alarms"])
    assert window.device_labels[role].text() == "●  连接断开"
    assert snapshot["metrics"]["quality"] == "degraded"
    assert snapshot["metrics"]["total_voltage"] == pytest.approx(4.1)
    assert snapshot["metrics"]["power_kw"] is None
    reading(controller, role, 100 if role == "current" else 2, 5)
    snapshot = controller.snapshot_at(5.02)
    assert not any(a["key"] == f"{role}_offline" for a in snapshot["alarms"])
    assert window.device_labels[role].text() == "●  已连接"


def test_allowed_long_timeout_is_not_misreported_before_request_can_finish(monitoring):
    controller, _ = monitoring
    controller.settings["ports"]["current"]["timeout"] = 2
    reading(controller, "current", 100, 2.1)
    assert not any(a["key"] == "current_offline" for a in controller.snapshot_at(5.5)["alarms"])
    assert any(a["key"] == "current_offline" for a in controller.snapshot_at(5.62)["alarms"])
