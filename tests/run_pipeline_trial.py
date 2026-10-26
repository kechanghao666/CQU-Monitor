"""独立寄存器夹具验证实际隔离进程全链路；非实物测试，不访问串口。"""

from dataclasses import dataclass, field
from pathlib import Path
import json
import math
import time
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
import cqu_monitor.controller as module  # noqa: E402
from cqu_monitor.config import default_settings  # noqa: E402
from cqu_monitor.ui import MainWindow  # noqa: E402
from device_fixtures import FixtureMeter, FixtureBus  # noqa: E402


@dataclass
class Clock:
    speed: float = 4
    origin: float = field(default_factory=time.monotonic)

    def now(self):
        return (time.monotonic() - self.origin) * self.speed


def main():
    import argparse
    from datetime import datetime

    parser = argparse.ArgumentParser(description="完整进程夹具验证，非实测，不打开串口")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts" / ("pipeline_" + datetime.now().strftime("%Y%m%d_%H%M%S")),
    )
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    module.ExperimentClock = Clock
    module.SerialProducer = FixtureMeter
    module.PulseBusProducer = FixtureBus
    settings = default_settings()
    settings["input_source"] = "外部寄存器夹具，非实测"
    window = MainWindow(settings)
    c = module.Controller(window, settings, output / "data")
    c.start()
    requested = False
    timer = QTimer()

    def watch():
        nonlocal requested
        if c.clock.now() >= 212 and not requested:
            requested = True
            c.request_stop()
        if requested and not c.running:
            timer.stop()
            try:
                sid = c.session_id
                rows = c.store.query_samples(sid, end=210, limit=10000)
                summaries = c.store.query_summaries(sid, end=210, limit=10000)
                controls = c.store.query_controls(sid, end=210, limit=10000)
                cells = {
                    i: [r for r in rows if r["valid"] and r["cell"] == i] for i in range(1, 31)
                }
                means = all(
                    v
                    and math.isclose(
                        v[-1]["mean_value"],
                        math.fsum(x["value"] for x in v) / len(v),
                        rel_tol=1e-12,
                    )
                    for v in cells.values()
                )
                voltages = [r for r in rows if r["valid"] and r["role"] != "current"]
                windows = all(
                    2 <= r["sync_evidence"]["phase_min"] <= r["sync_evidence"]["phase_max"] < 4
                    for r in voltages
                )
                power = [s["power_kw"] for s in summaries if s["power_kw"] is not None]
                avg = bool(power) and math.isclose(
                    summaries[-1]["avg_power_kw"], math.fsum(power) / len(power), rel_tol=1e-12
                )

                def boundary(role, valid, a, b):
                    return any(
                        r["role"] == role and r["valid"] == valid and a <= r["elapsed"] < b
                        for r in rows
                    )

                checks = dict(
                    all_30_cells=all(len(v) >= 2 for v in cells.values()),
                    means=means,
                    whole_request_in_window=windows,
                    left_fault_excluded=boundary("voltage_left", False, 44, 47),
                    right_continued=boundary("voltage_right", True, 44, 47),
                    current_continued=boundary("current", True, 44, 47),
                    current_fault_power_missing=any(
                        s["power_kw"] is None and 26 <= s["elapsed"] <= 28 for s in summaries
                    ),
                    frozen_excluded=any(
                        r.get("sync_evidence", {}).get("status") in ("frozen", "inconsistent")
                        for r in rows
                        if r["role"] == "voltage_left"
                    ),
                    power_average=avg,
                    recorder_drained=c.recorder.pending_count == 0,
                    all_processes_exited=all(
                        not w.is_alive() and not w.process.is_alive() for w in c.workers
                    ),
                    no_controller_error=c.last_error is None,
                )
                for suffix in (".txt", ".xlsx"):
                    c.store.export_session(sid, output / ("history" + suffix), valid_only=False)
                    c.store.export_snapshot(c.capture_snapshot(), output / ("snapshot" + suffix))
                report = dict(
                    passed=all(checks.values()),
                    checks=checks,
                    virtual_seconds=210,
                    speed=4,
                    physical_seconds=(time.monotonic() - c.clock.origin),
                    meter_readings=len(rows),
                    controls=len(controls),
                    summaries=len(summaries),
                    valid_counts={i: len(v) for i, v in cells.items()},
                    real_ports_opened=0,
                    real_device_tested=False,
                )
                (output / "report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps(report, ensure_ascii=False))
                app.exit(0 if report["passed"] else 1)
            except Exception as exc:
                (output / "failure.txt").write_text(repr(exc), encoding="utf-8")
                app.exit(1)

    timer.timeout.connect(watch)
    timer.start(50)
    return app.exec()


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    sys.exit(main())
