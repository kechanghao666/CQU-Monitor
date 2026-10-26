"""真实时间1Hz寄存器夹具：内存平台、报警、缓存及停止重开。

仅开发测试，不接真实串口；虚拟曲线压力点不写入SQLite。
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
import gc
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from device_fixtures import FixtureBus, FixtureClient, FixtureMeter, Reply  # noqa: E402
import serial  # noqa: E402


def forbidden_serial_open(*args, **kwargs):
    path = os.environ.get("CQU_TRIAL_SERIAL_GUARD_LOG")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"blocked pid={os.getpid()}\n")
    raise RuntimeError("寄存器夹具禁止真实串口")


serial.Serial.open = forbidden_serial_open


@dataclass
class Clock:
    speed: float = 1
    origin: float = field(default_factory=time.monotonic)

    def now(self):
        return time.monotonic() - self.origin


class SoakClient(FixtureClient):
    def read_holding_registers(self, address, count, unit):
        if self.role == "current" and 12 <= self.clock.now() < 19:
            return Reply([5800])
        return super().read_holding_registers(address, count, unit)


class SoakMeter(FixtureMeter):
    def __init__(self, role, parameters, output, clock, stop_event):
        super().__init__(role, parameters, output, clock, stop_event)
        self.client_factory = lambda **kwargs: SoakClient(role, clock)


class Counters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
        (key, ctypes.c_size_t)
        for key in (
            "peak_ws",
            "ws",
            "peak_paged",
            "paged",
            "peak_nonpaged",
            "nonpaged",
            "pagefile",
            "peak_pagefile",
            "private",
        )
    ]


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]


def memory(pid):
    process = kernel.OpenProcess(0x410, False, pid)
    if not process:
        return None
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        handles = wintypes.DWORD()
        if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            return None
        kernel.GetProcessHandleCount(process, ctypes.byref(handles))
        return dict(
            pid=pid,
            private_bytes=counters.private,
            working_set_bytes=counters.ws,
            handles=handles.value,
        )
    finally:
        kernel.CloseHandle(process)


def hashes():
    return {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest().upper()
        for p in sorted((ROOT / "cqu_monitor").rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=1200)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.environ["CQU_TRIAL_SERIAL_GUARD_LOG"] = str(out / "blocked_serial_attempts.log")
    logging.basicConfig(
        filename=out / "application.log",
        level=logging.INFO,
        encoding="utf-8",
        format="%(asctime)s %(levelname)s %(message)s",
    )
    from PySide6.QtCore import QObject, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton
    import cqu_monitor.controller as module
    from cqu_monitor import __version__
    from cqu_monitor.config import default_settings
    from cqu_monitor.ui import MainWindow

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    module.ExperimentClock = Clock
    module.SerialProducer, module.PulseBusProducer = SoakMeter, FixtureBus
    settings = default_settings()
    settings["input_source"] = f"{__version__}内存与功能测试寄存器夹具，非实测"
    settings["current_high"] = 135
    window = MainWindow(settings)
    window.setWindowTitle(f"{__version__} 内存与功能试运行（寄存器夹具，非实测）")
    banner = QLabel("测试夹具 · 非实测 · 禁止真实串口；曲线含缓存压力点")
    banner.setStyleSheet("background:#fff0bf;color:#8a4f00;padding:8px;")
    window.statusBar().addPermanentWidget(banner, 1)
    window.show()
    window.tabs.setCurrentIndex(1)
    notice = window.alarm_notifier
    audio = dict(plays=0, stops=0, confirmations=0)

    class SilentSound:
        def play(self):
            audio["plays"] += 1

        def stop(self):
            audio["stops"] += 1

    notice.sound = SilentSound()
    c = module.Controller(window, settings, out / "data")
    source_before = hashes()
    c.start()
    assert c.running, c.last_error
    charts = [*window.cell_charts.values(), *window.summary_charts.values()]
    started = time.monotonic()
    phase, frames = "pressure_and_acquisition", 0
    checkpoints, errors = [], []
    alarms_seen, unexpected = set(), []
    pressure_complete = False
    last_minute = -1
    first_result = None
    restart_started = None
    stopped_at = None
    heartbeat_count = 0

    def telemetry(label=None):
        children = [w.process.pid for w in c.workers if w.process is not None and w.process.pid]
        processes = [m for pid in [os.getpid(), *children] if (m := memory(pid))]
        row = dict(
            time=datetime.now().astimezone().isoformat(),
            wall_seconds=time.monotonic() - started,
            acquisition_seconds=c.clock.now(),
            phase=phase,
            label=label,
            pid=os.getpid(),
            child_pids=children,
            private_bytes=processes[0]["private_bytes"],
            working_set_bytes=processes[0]["working_set_bytes"],
            total_private_bytes=sum(m["private_bytes"] for m in processes),
            total_working_set_bytes=sum(m["working_set_bytes"] for m in processes),
            total_handles=sum(m["handles"] for m in processes),
            processes=processes,
            qobjects=len(window.findChildren(QObject)),
            python_objects=len(gc.get_objects()),
            allocated_blocks=sys.getallocatedblocks(),
            python_threads=threading.active_count(),
            frames_per_chart=frames,
            points_min=min(len(ch.times) for ch in charts),
            points_max=max(len(ch.times) for ch in charts),
            log_blocks=window.event_log.document().blockCount(),
            queue=c.output.qsize(),
            recorder_pending=c.recorder.pending_count,
            pending_voltage=len(c.pending_voltage),
            unrecorded=len(c.unrecorded),
            controller_history={g: len(h) for g, h in c.pulse.history.items()},
        )
        with (out / "memory_samples.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        if label:
            checkpoints.append(row)
            (out / "checkpoints.json").write_text(
                json.dumps(checkpoints, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return row

    def pressure_chunk():
        nonlocal frames, phase, pressure_complete
        for _ in range(5):
            t = frames - 2200
            for chart in charts:
                chart.append(t, 1.8, 1.8)
            frames += 1
        if frames >= 2200:
            pressure_complete = True
            phase = "acquisition"
            telemetry("cache_saturated")
            print("cache_saturated", frames, flush=True)
        else:
            QTimer.singleShot(1, pressure_chunk)

    def collect_first_result():
        rows = c.store.query_samples(c.session_id, limit=100000)
        controls = c.store.query_controls(c.session_id, limit=100000)
        summaries = c.store.query_summaries(c.session_id, limit=100000)
        valid = [r for r in rows if r["valid"] and r["cell"]]
        cells = {r["cell"] for r in valid}
        checks = dict(
            no_acquisition_error=not c.last_error and not errors,
            all_30_cells=len(cells) == 30,
            all_voltage_requests_inside_window=all(
                2 <= r["sync_evidence"]["phase_min"] <= r["sync_evidence"]["phase_max"] < 4
                for r in valid
            ),
            chart_buffers_saturated_and_bounded=pressure_complete
            and all(len(ch.times) == 2000 for ch in charts),
            logs_bounded=window.event_log.document().blockCount() <= 300,
            control_history_bounded=all(len(h) == 150 for h in c.pulse.history.values()),
            all_queues_drained=c.output.empty()
            and not c.pending_voltage
            and c.recorder.pending_count == 0,
            all_readers_exited=all(
                not w.is_alive() and not w.process.is_alive() for w in c.workers
            ),
            no_recovery_payload=not c.unrecorded,
            no_unexpected_alarm=not unexpected,
            real_ports_blocked=not (out / "blocked_serial_attempts.log").exists(),
            acquisition_reached_duration=c.clock.now() >= args.seconds,
            actual_threshold_and_disconnect_and_freeze_seen={
                "current_high",
                "current_offline",
                "controller_left",
            }
            <= alarms_seen,
        )
        window.grab().save(str(out / "持续采集结束_测试夹具.png"))
        result = dict(
            session_id=c.session_id,
            samples=len(rows),
            controls=len(controls),
            summaries=len(summaries),
            valid_cells=sorted(cells),
            checks=checks,
            acquisition_seconds=c.clock.now(),
            alarms_seen=sorted(alarms_seen),
            unexpected_alarms=unexpected,
        )
        c.store.export_snapshot(c.capture_snapshot(), out / "当前页面.txt")
        c.store.export_snapshot(c.capture_snapshot(), out / "当前页面.xlsx")
        c.store.export_session(c.session_id, out / "历史记录.txt", valid_only=False)
        c.store.export_session(c.session_id, out / "历史记录.xlsx", valid_only=False)
        result["checks"]["txt_and_xlsx_exports_created"] = all(
            (out / n).stat().st_size > 0
            for n in ("当前页面.txt", "当前页面.xlsx", "历史记录.txt", "历史记录.xlsx")
        )
        return result

    def watch():
        nonlocal heartbeat_count, last_minute, phase, first_result, restart_started, stopped_at
        heartbeat_count += 1
        elapsed = c.clock.now()
        if c.last_snapshot and phase in ("pressure_and_acquisition", "acquisition"):
            for alarm in c.last_snapshot["alarms"]:
                key = alarm["key"]
                alarms_seen.add(key)
                allowed = (
                    (key == "current_high" and 11.9 <= elapsed <= 20.5)
                    or (key in ("current_offline", "current_stale") and 24.9 <= elapsed <= 29.5)
                    or (
                        key in ("controller_left", "voltage_quality")
                        and (43.9 <= elapsed <= 47.5 or 69.9 <= elapsed <= 73.5)
                    )
                )
                if not allowed and (not unexpected or unexpected[-1]["key"] != key):
                    unexpected.append(dict(key=key, elapsed=elapsed, alarm=alarm))
        if notice.dialog.isVisible():
            if not (out / "报警弹窗_测试夹具.png").exists():
                notice.dialog.grab().save(str(out / "报警弹窗_测试夹具.png"))
            assert notice.hint.text() == "请检查设备和测量数值"
            confirm = notice.dialog.findChildren(QPushButton)
            assert len(confirm) == 1 and confirm[0].text() == "确认"
            confirm[0].click()
            audio["confirmations"] += 1
            assert not notice.timer.isActive()
        if phase in ("pressure_and_acquisition", "acquisition"):
            minute = int(elapsed // 60)
            if minute > last_minute:
                last_minute = minute
                telemetry(f"acquisition_{minute}min")
                print(f"acquisition {minute}min", flush=True)
            if c.last_error or not c.running:
                errors.append(c.last_error or "采集意外停止")
                phase = "stopping"
                c.request_stop(status="error")
            elif elapsed >= args.seconds and pressure_complete:
                telemetry("acquisition_end")
                phase = "stopping"
                stopped_at = time.monotonic()
                c.request_stop()
        elif phase == "stopping" and not c.running:
            telemetry("stopped_before_export")
            first_result = collect_first_result()
            phase = "restart"
            c.start()
            assert c.running and c.session_id != first_result["session_id"]
            assert all(cell["count"] == 0 for cell in c.engine.snapshot(0)["cells"])
            restart_started = time.monotonic()
        elif phase == "restart" and time.monotonic() - restart_started >= 8:
            phase = "restart_stopping"
            c.request_stop()
        elif phase == "restart_stopping" and not c.running:
            phase = "finished"
            checks = first_result["checks"]
            checks["restart_new_session_and_clean_stop"] = not c.last_error and all(
                not w.is_alive() and not w.process.is_alive() for w in c.workers
            )
            checks["production_source_unchanged_during_trial"] = source_before == hashes()
            checks["confirmation_muted_sound"] = audio["confirmations"] > 0 and audio["stops"] > 0
            telemetry("finished")
            report = dict(
                version=__version__,
                passed=all(checks.values()),
                first_session=first_result,
                checks=checks,
                audio_test_double=audio,
                requested_acquisition_seconds=args.seconds,
                total_test_seconds=time.monotonic() - started,
                event_loop_heartbeats=heartbeat_count,
                source_hashes=source_before,
                real_ports_opened=0,
                real_hardware_tested=False,
                physical_24h_tested=False,
                memory_numeric_limit_specified=False,
                errors=errors,
            )
            (out / "run-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(dict(passed=report["passed"], checks=checks), ensure_ascii=False),
                flush=True,
            )
            monitor.stop()
            timer.stop()
            window.close()
            app.exit(0 if report["passed"] else 1)

    timer = QTimer()
    timer.timeout.connect(watch)
    timer.start(100)
    monitor = QTimer()
    monitor.timeout.connect(telemetry)
    monitor.start(5000)
    telemetry("start")
    QTimer.singleShot(0, pressure_chunk)
    return app.exec()


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    sys.exit(main())
