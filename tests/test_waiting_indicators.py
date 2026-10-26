"""已取得可靠等待状态时，文字提示须与熄灭的指示灯及非报警状态一致。"""

import pytest

from cqu_monitor.config import default_settings
from cqu_monitor.ui import MainWindow


@pytest.mark.parametrize(
    "stage,caption", [("interval", "切换间隔"), ("cycle_wait", "周期等待"), ("stopped", "未接通")]
)
def test_confirmed_waiting_has_specific_caption(qtbot, stage, caption):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    window.set_running(True)
    window._set_measurement_indicators(
        controllers={
            "left": {"stage": stage, "status": "waiting", "active": False, "cell": 1},
            "right": {"stage": stage, "status": "waiting", "active": False, "cell": 16},
        }
    )
    assert window.current_measurement_label.text() == f"当前测量：A组 {caption}  ｜  B组 {caption}"
    assert window.chart_measurement_label.text() == window.current_measurement_label.text()
    assert not any(lamp.property("active") for lamp in window.cell_indicators.values())
