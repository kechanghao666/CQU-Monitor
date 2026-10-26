"""结构化控制记录须保留原始寄存器中的间隔倒计时，避免导出列始终留空。"""

import pytest

from cqu_monitor.config import default_settings
from cqu_monitor.pulse import PulseTracker


@pytest.mark.parametrize("interval,cycle", [(2, 0), (0, 120)])
def test_waiting_countdowns_are_retained_as_exportable_fields(interval, cycle):
    tracker = PulseTracker(default_settings()["controllers"])
    observation = tracker.ingest(
        dict(
            group="left",
            started=2,
            finished=2.1,
            parameters=[600, 2, 120, 15],
            registers=[1, 0, 0, interval, 3, cycle],
            error=None,
        )
    )
    assert observation.get("interval_remaining") == interval
    assert observation.get("cycle_remaining") == cycle
    assert observation["raw_registers"] == [1, 0, 0, interval, 3, cycle]
    assert tracker.snapshot(2.11)["left"]["alarm"] is False
