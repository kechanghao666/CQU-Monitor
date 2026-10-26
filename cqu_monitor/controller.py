"""连接界面、计算核心和记录器；状态变更只发生在Qt主线程。"""

from copy import deepcopy
from datetime import datetime, timedelta
import logging
import json
import os
from pathlib import Path
from queue import Empty, Queue
import threading
import tempfile
import time

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from .config import ROLE_NAMES, ROLES, save_settings, validate_settings
from .engine import Engine
from .storage import QueuedRecorder, SessionStore
from .workers import ExperimentClock, SerialProducer
from .pulse import PulseTracker
from .pulse_workers import PulseBusProducer
from .isolated import IsolatedProducer

LOGGER = logging.getLogger(__name__)


class ExportTask(QThread):
    """导出线程只接收深复制快照，避免后台工作读取正在变化的界面或引擎。"""

    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, store, snapshot, destination, parent=None):
        super().__init__(parent)
        self.store, self.snapshot, self.destination = store, deepcopy(snapshot), destination

    def run(self):
        try:
            self.store.export_snapshot(self.snapshot, self.destination)
            self.completed.emit(str(self.destination))
        except Exception as exc:
            self.failed.emit(str(exc))


class Controller(QObject):
    """主线程协调器：串行判定采集包、更新计算与界面、提交异步记录器。"""

    def __init__(self, window, settings, data_dir, launch_workers=True):
        super().__init__()
        self.window, self.settings = window, deepcopy(settings)
        self.data_dir = Path(data_dir)
        self.store = SessionStore(self.data_dir / "measurements.sqlite3")
        self.store.mark_interrupted_sessions()
        self.launch_workers = launch_workers
        self.engine = self.recorder = self.clock = None
        self.session_id = None
        self.running = self.stopping = False
        self.output = Queue(maxsize=10000)
        self.workers, self.exports = [], []
        self.statuses = dict.fromkeys(ROLES, "stopped")
        self.meter_responses = dict.fromkeys(ROLES)
        self.silent_meters = {}
        self.active_alarms = set()
        self.last_snapshot = None
        self.last_render = -1
        self.last_sample_time = 0.0
        self.last_error = None
        self.pulse = None
        self.pending_voltage = []
        self.unrecorded = []
        self.persistence_error = None
        self.recovery_path = None
        self.stop_pending = False
        self._close_requested = False
        self._save_thread = None
        self._stop_error_lock = threading.Lock()
        self.unsafe_to_close = False
        self.timer = QTimer(self)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.poll)
        for signal, callback in [
            ("start_requested", self.start),
            ("stop_requested", self.request_stop),
            ("sync_requested", self.synchronize),
            ("settings_requested", self.configure),
            ("export_requested", self.export),
            ("history_requested", self.history),
            ("close_requested", self.request_shutdown),
        ]:
            getattr(window, signal).connect(callback)
        self._show_devices()

    def _show_devices(self):
        """连接卡合并真实状态包与无响应监测，避免旧的已连接状态掩盖驱动阻塞。"""
        self.window.set_devices(
            {
                role: "disconnected" if role in self.silent_meters else state
                for role, state in self.statuses.items()
            }
        )

    def _refresh_meter_health(self, now):
        """三秒首次回包宽限；后续允许1Hz间隔及配置超时，但禁止无限等待。

        以实际读取完成包为依据，正常电压采样窗口外的成功读取也算响应。
        停止后冻结最后质量，不因保存用时制造新报警；离线纯计算测试不启用。
        """
        if not self.launch_workers or not self.running or self.stopping:
            return
        silent = {}
        for role in ROLES:
            finished = self.meter_responses[role]
            limit = (
                3.0 if finished is None else max(2.5, self.settings["ports"][role]["timeout"] + 1.5)
            )
            expired = now - (finished if finished is not None else 0) > limit
            self.engine.set_meter_responsive(role, not expired)
            if expired:
                silent[role] = (
                    f"{ROLE_NAMES[role]}超过{limit:g}秒未返回读取结果，请检查设备、驱动及连线"
                )
        if silent != self.silent_meters:
            self.silent_meters = silent
            self._show_devices()

    def wall_time(self, elapsed=None):
        """把实验单调偏移映射到带时区墙钟，保持数据先后与显示时间各司其职。"""
        if elapsed is None:
            elapsed = self.clock.now() if self.clock else 0
        return (self.wall_origin + timedelta(seconds=elapsed)).isoformat(timespec="milliseconds")

    def record_event(self, level, message, persist=True):
        """显示并按需保存运行事件；停止后的提示可只写日志，避免再次提交已关闭记录器。"""
        wall = self.wall_time() if self.clock else datetime.now().astimezone().isoformat()
        self.window.append_event(wall, level, message)
        getattr(LOGGER, "error" if level == "error" else "info")(message)
        if persist and self.recorder and self.running:
            self.queue_record(
                "event",
                dict(session_id=self.session_id, wall_time=wall, level=level, message=message),
            )

    def queue_record(self, kind, payload):
        """提交持久化任务；提交失败保留原载荷并阻止继续正常采集。"""
        try:
            self.recorder.submit(kind, payload)
        except Exception as exc:
            # 记录器拒绝的尾部结果仍由控制器保留；停止生产后统一写恢复文件。
            self.unrecorded.append((kind, deepcopy(payload)))
            self.persistence_error = str(exc)

    def save_unrecorded(self):
        """将未投递载荷写入独立恢复文件；该材料需人工核对，不自动导回数据库。"""
        if not self.unrecorded:
            return
        directory = self.data_dir / "recovery"
        directory.mkdir(parents=True, exist_ok=True)
        self.recovery_path = directory / f"undelivered-{self.session_id}.jsonl"
        descriptor, temporary = tempfile.mkstemp(
            prefix="undelivered-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                for kind, payload in self.unrecorded:
                    handle.write(
                        json.dumps(
                            dict(kind=kind, payload=payload), ensure_ascii=False, allow_nan=False
                        )
                        + "\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.recovery_path)
        finally:
            # 重试写盘失败时仍保住上一份完整恢复文件，只清理本次临时文件。
            Path(temporary).unlink(missing_ok=True)

    def start(self):
        """校验配置并建立新会话、引擎、记录器和读取者；不沿用上次实验统计。"""
        if self.running or self.stopping or self.unsafe_to_close:
            return
        try:
            validate_settings(self.settings, for_start=True)
            self.wall_origin = datetime.now().astimezone()
            self.clock = ExperimentClock()
            self.output = Queue(maxsize=10000)
            self.stop_event = threading.Event()
            self.session_id = self.store.create_session(
                self.settings["mode"], self.settings, self.wall_time(0)
            )
            self.engine = Engine(self.settings, self.session_id)
            self.engine.start(0, self.wall_time(0))
            self.pulse = (
                PulseTracker(self.settings["controllers"], self.settings)
                if self.settings.get("synchronization", "manual") == "controller"
                else None
            )
            self.pending_voltage = []
            self.meter_responses = dict.fromkeys(ROLES)
            self.silent_meters = {}
            self.statuses = dict.fromkeys(
                (*ROLES, "controller_left", "controller_right") if self.pulse else ROLES,
                "stopped",
            )
            self.recorder = QueuedRecorder(self.store, self.session_id)
            self.recorder.start()
            self.running = True
            self.last_error = None
            self.unrecorded, self.persistence_error, self.recovery_path = [], None, None
            self.last_render, self.last_sample_time = -1, 0.0
            self.active_alarms.clear()
            self.window.reset_session(self.settings["mode"])
            self.workers = []
            if self.launch_workers:
                planned_workers = [
                    IsolatedProducer(
                        SerialProducer,
                        (role, self.settings["ports"][role]),
                        self.output,
                        self.clock,
                        self.stop_event,
                        name=f"meter-{role}",
                    )
                    for role in ROLES
                ]
                if self.pulse:
                    buses = {}
                    for group, profile in self.settings["controllers"].items():
                        name = profile["port"].strip().upper()
                        normalized = {**profile, "port": name}
                        buses.setdefault(name, {})[group] = normalized
                    planned_workers.extend(
                        IsolatedProducer(
                            PulseBusProducer,
                            (groups,),
                            self.output,
                            self.clock,
                            self.stop_event,
                            name=f"controller-{name}",
                        )
                        for name, groups in buses.items()
                    )
                for worker in planned_workers:
                    worker.start()
                    self.workers.append(worker)
            self.window.set_running(True, waiting_sync=self.pulse is None)
            self.statuses = dict.fromkeys(self.statuses, "connecting")
            self._show_devices()
            self.record_event(
                "info",
                "新实验开始；电流持续记录，左右电压由各自控制器输出状态自动同步"
                if self.pulse
                else "新实验开始；电流持续记录，电压等待同步到1和16号小室",
            )
            self.timer.start()
            self.poll()
        except Exception as exc:
            self.fail(f"无法开始实验：{exc}")

    def synchronize(self):
        """后续硬件触发适配器也调用此入口；首版按钮是人工确认的零点。"""
        if not self.running or self.pulse is not None:
            return
        try:
            self.drain()
            now = self.clock.now()
            self.engine.synchronize(now)
            for worker in self.workers:
                if isinstance(worker, SerialProducer) or (
                    isinstance(worker, IsolatedProducer) and worker.worker_type is SerialProducer
                ):
                    worker.schedule_origin = now
            self.window.set_running(True, waiting_sync=False)
            self.record_event(
                "info", "人工同步：确认继电器此刻连接1和16号；从重新测到的首个小室开始合计总压"
            )
            self.render()
        except Exception as exc:
            self.fail(f"同步失败：{exc}")

    def drain(self, final=False, defer_voltage=False, max_items=10000):
        # 每次最多处理一整队；停止时生产者已退出，因此不会留下尾部样本。
        """有界排空主结果队列；收尾中坏包保留原始证据，后续正常包继续处理。"""
        for _ in range(max_items):
            try:
                item = self.output.get_nowait()
            except Empty:
                break
            try:
                self._consume_packet(item)
            except Exception as exc:
                self.unrecorded.append(
                    ("raw_packet", dict(session_id=self.session_id, raw=repr(item), error=str(exc)))
                )
                if not self.stopping:
                    raise
                self._note_stop_error(f"末尾记录处理失败：{exc}")
        if not defer_voltage and not self.stop_event.is_set():
            self.flush_pending(final=final)

    def _consume_packet(self, item):
        """按包类型分派连接状态、控制器观测和仪表尝试；不在设备进程中计算均值。"""
        if item["kind"] == "device":
            role, state = item["role"], item["state"]
            if self.statuses.get(role) != state:
                previous = self.statuses.get(role)
                self.statuses[role] = state
                self._show_devices()
                if state == "disconnected":
                    self.record_event(
                        "error", f"{ROLE_NAMES[role]}掉线或读取失败，请检查连线及参数"
                    )
                elif state == "connected" and previous == "disconnected":
                    self.record_event("info", f"{ROLE_NAMES[role]}读取恢复")
        elif item["kind"] == "controller":
            if self.pulse is None:
                return
            observation = self.pulse.ingest(item)
            parameters = item.get("parameters")
            if (
                not observation.get("error")
                and observation.get("outputs") == 15
                and isinstance(parameters, (list, tuple))
                and len(parameters) == 4
                and all(type(value) is int and 0 <= value <= 65535 for value in parameters)
                and parameters[0] == round(self.engine.step_seconds * 100)
            ):
                self.engine.set_controller_intervals(item["group"], parameters[1], parameters[2])
            finished = item["finished"]
            self.last_sample_time = max(self.last_sample_time, finished)
            observation.update(
                session_id=self.session_id, elapsed=finished, wall_time=self.wall_time(finished)
            )
            self.queue_record("control", observation)
        elif item["kind"] == "reading":
            self.last_sample_time = max(self.last_sample_time, item["finished"])
            self.meter_responses[item["role"]] = item["finished"]
            if self.pulse is not None and item["role"] != "current":
                self.pending_voltage.append(item)
            else:
                self.accept_reading(item)

    def accept_reading(self, item, context=None):
        """先形成并提交完整样本，再更新曲线；显示异常不能使已取得样本静默丢失。"""
        sample = self.engine.accept(
            item["role"],
            item["value"],
            item["started"],
            item["finished"],
            self.wall_time(item["finished"]),
            error=item.get("error"),
            sync_context=context,
        )
        self.queue_record("sample", sample)
        try:
            self.window.add_sample(sample)
        except Exception as exc:
            if not self.stopping:
                raise
            self._note_stop_error(f"末尾数据显示失败（原样本仍保存）：{exc}")

    def flush_pending(self, final=False):
        """逐项判断等待后侧证据的电压包；最终收尾不再等待，错误项另留恢复证据。"""
        if self.pulse is None:
            return
        now = max(self.clock.now(), self.last_sample_time)
        waiting = []
        original = self.pending_voltage
        self.pending_voltage = []
        for index, item in enumerate(original):
            try:
                group = "left" if item["role"] == "voltage_left" else "right"
                context = self.pulse.assess(
                    group, item["started"], item["finished"], now, final=final
                )
                if context is None:
                    if final:
                        raise RuntimeError("最终电压判定仍缺少结果")
                    waiting.append(item)
                else:
                    self.accept_reading(item, context)
            except Exception as exc:
                if not self.stopping:
                    self.pending_voltage = waiting + original[index:]
                    raise
                self.unrecorded.append(
                    ("raw_packet", dict(session_id=self.session_id, raw=repr(item), error=str(exc)))
                )
                self._note_stop_error(f"末尾电压判定失败，原始请求已保留：{exc}")
        self.pending_voltage = waiting

    def snapshot_at(self, now):
        """把引擎快照与两组同步状态合并，供界面、导出及最终保存共用。"""
        self._refresh_meter_health(now)
        states = self.pulse.snapshot(now) if self.pulse else None
        if states is not None:
            for group, state in states.items():
                self.engine.set_controller_health(group, state["healthy"])
        snapshot = self.engine.snapshot(now)
        for role, reason in self.silent_meters.items():
            # 之前的失败包可能已生成同一报警；无响应原因替换它，不重复显示或记账。
            snapshot["alarms"] = [
                alarm for alarm in snapshot["alarms"] if alarm["key"] != f"{role}_offline"
            ]
            snapshot["alarms"].append(
                dict(key=f"{role}_offline", level="error", condition="no_response", message=reason)
            )
        if states is not None:
            snapshot["controllers"] = states
            snapshot["cell_pair"] = [s["cell"] for s in states.values() if s["active"]]
            snapshot["synchronized"] = any(s["active"] for s in states.values())
            for group, state in states.items():
                if state["alarm"]:
                    snapshot["alarms"].append(
                        dict(
                            key=f"controller_{group}",
                            level="warning",
                            condition=state["status"],
                            message=f"{'左' if group == 'left' else '右'}组：{state['reason']}",
                        )
                    )
        return snapshot

    def render(self):
        """显示最新快照，并将新报警与恢复按状态边沿记录，避免每次刷新重复报警。"""
        self.last_snapshot = self.snapshot_at(max(self.clock.now(), self.last_sample_time))
        self.window.render(self.last_snapshot)
        present = {alarm["key"] for alarm in self.last_snapshot["alarms"]}
        for alarm in self.last_snapshot["alarms"]:
            if alarm["key"] not in self.active_alarms:
                self.record_event(alarm["level"], alarm["message"])
        for key in self.active_alarms - present:
            # 超限报警消失也可能因为读数失效；只有再次取得可用读数才能称为恢复。
            if key.startswith("current_") and self.last_snapshot["metrics"]["current"] is None:
                self.record_event("warning", f"报警状态无法判定（电流读数无效）：{key}")
            else:
                self.record_event("info", f"报警恢复：{key}")
        self.active_alarms = present

    def check_failures(self):
        failures = [worker.failure for worker in self.workers if worker.failure]
        if failures or self.recorder.error or self.persistence_error:
            raise RuntimeError(
                "; ".join(failures) or str(self.recorder.error or self.persistence_error)
            )

    def poll(self):
        """Qt定时入口：正常采集时排包、统计及节流显示，停止时转入异步收尾。"""
        if not self.running:
            return
        if self.stopping:
            self._poll_stop()
            return
        try:
            # 已有故障先停产汇尾包；drain期间新发生的故障也不能抢先结算电压。
            self.check_failures()
            self.drain()
            self.check_failures()
            now = max(self.clock.now(), self.last_sample_time)
            self.snapshot_at(now)
            summary = self.engine.tick(now, self.wall_time(now))
            if summary is not None:
                self.queue_record("summary", summary)
                self.window.add_summary(summary)
                self.queue_record("snapshot", self.snapshot_at(now))
            # 界面最多约10次/秒；统计及读取仍按独立实验时间推进。
            real_elapsed = self.clock.now()
            if real_elapsed - self.last_render >= 0.1:
                self.render()
                self.last_render = real_elapsed
        except Exception as exc:
            self.fail(f"采集已停止：{exc}")

    def request_stop(self, status="completed", reason=None):
        """界面入口只发出停止意图；线程等待和写库均不占用Qt事件循环。"""
        if not self.running or self.stopping:
            return
        self.stopping = True
        self.stop_pending = True
        self._stop_status = status
        self._stop_failure = None
        self._seen_stop_errors = set()
        self._late_stop_events = []
        self._save_thread = None
        self._tails_loaded = False
        self.stop_event.set()
        # 在首次排包或启动保存线程之前冻结根因，避免无读取者时立即收尾的竞态。
        if reason:
            self._note_stop_error(str(reason))
        self.window._close_blocked = True
        if hasattr(self.window, "set_stopping"):
            self.window.set_stopping()
        self.record_event("info", "正在停止读取，等待串口退出；随后保存末尾数据")
        self.timer.start()
        self._poll_stop()

    def _merge_stop_failure(self, message):
        """后台保存错误只合并不覆盖，保护主线程在保存途中收到的新异常。"""
        if message:
            with self._stop_error_lock:
                self._stop_failure = (
                    f"{self._stop_failure}; {message}" if self._stop_failure else message
                )

    def _note_stop_error(self, message):
        """收尾错误去重；记录器关闭后收到的事件由最终关口另开事务补记。"""
        if message in self._seen_stop_errors:
            return
        self._seen_stop_errors.add(message)
        self._stop_status = "error"
        self._merge_stop_failure(message)
        LOGGER.error(message)
        event = dict(
            session_id=self.session_id,
            wall_time=self.wall_time(),
            level="error",
            message=message,
        )
        if self._save_thread is None:
            self.queue_record("event", event)
        else:
            # 此时submit可能已关闭；晚到异常不能进入没人再消费的普通队列。
            self._late_stop_events.append(event)

    def stop(self, status="completed"):
        """离线脚本兼容入口；生产界面使用request_stop，不在界面里等待。"""
        self.request_stop(status)
        if any(worker.is_alive() for worker in self.workers):
            return
        while self.stopping:
            self._poll_stop()
            if self.stopping:
                time.sleep(0.005)

    def _poll_stop(self):
        """按退出顺序推进停止，绝不在Qt主线程join读取者。

        必须依次完成读取者退出、主队列和尾包排空、待定电压判定，
        才显示保存阶段并交后台写盘；保存结束后才恢复待机和允许重开。
        """
        if self._save_thread is not None:
            if not self._save_thread.is_alive():
                if self._late_stop_events:
                    events, self._late_stop_events = self._late_stop_events, []
                    self._save_thread = threading.Thread(
                        target=self._save_late_stop_errors,
                        args=(events,),
                        daemon=True,
                        name="stop-diagnostics",
                    )
                    self._save_thread.start()
                else:
                    self._finish_stop()
            return
        try:
            self.drain(defer_voltage=True, max_items=200)
            if any(worker.is_alive() for worker in self.workers):
                return
            if not self.output.empty():
                return
            if not self._tails_loaded:
                for worker in self.workers:
                    while worker.pending and not self.output.full():
                        self.output.put_nowait(worker.pending.pop(0))
                    if worker.pending:
                        return
                    if getattr(worker, "failure", None):
                        self._note_stop_error(str(worker.failure))
                    if getattr(worker, "forced_stop", False):
                        if self._stop_status != "error":
                            self._stop_status = "interrupted"
                        self.record_event(
                            "warning",
                            f"{worker.name}串口驱动未及时退出，已取消独立串口进程；未完成的读取不纳入结果",
                        )
                self._tails_loaded = True
                if not self.output.empty():
                    return
            self.flush_pending(final=True)
            self.render()
            if self.persistence_error:
                raise RuntimeError(self.persistence_error)
            self.record_event("info", "读取已停止，正在提交末尾数据")
        except Exception as exc:
            self._note_stop_error(str(exc))
            if (
                any(worker.is_alive() for worker in self.workers)
                or not self.output.empty()
                or not self._tails_loaded
            ):
                return
        if self.engine:
            self.last_snapshot = self.snapshot_at(max(self.clock.now(), self.last_sample_time))
        ended_at = self.wall_time()
        if hasattr(self.window, "set_saving"):
            self.window.set_saving()
        self._save_thread = threading.Thread(
            target=self._save_stop, args=(ended_at,), daemon=True, name="stop-save"
        )
        self._save_thread.start()

    def _save_stop(self, ended_at):
        """后台完成记录器关闭与未投递恢复；失败明确回传，不能冒充已全部保存。"""
        failure = None
        try:
            if self.recorder:
                self.recorder.close(self.last_snapshot, ended_at, status=self._stop_status)
        except Exception as exc:
            failure = f"{failure + '; ' if failure else ''}保存失败：{exc}"
            if self.recorder and (
                self.recorder.alive or "恢复文件写入也失败" in str(self.recorder.error)
            ):
                self.unsafe_to_close = True
        try:
            self.save_unrecorded()
            if self.recovery_path:
                failure = f"{failure or '保存异常'}；未投递数据恢复文件：{self.recovery_path}"
        except Exception as exc:
            failure = f"{failure or '保存异常'}；恢复文件也无法写入：{exc}，数据仍保留在内存"
            self.unsafe_to_close = True
        self._merge_stop_failure(failure)

    def _save_late_stop_errors(self, events):
        """保存线程结束后补记晚到异常；仍有新事件则下一次轮询继续补记。"""
        try:
            if self.recorder and self.recorder.alive:
                raise RuntimeError("记录器尚未退出，晚到异常改存恢复文件")
            self.store.record_session_errors(self.session_id, events)
        except Exception as exc:
            self.unrecorded.extend(("event", event) for event in events)
            self._merge_stop_failure(f"晚到异常保存失败：{exc}")
            try:
                self.save_unrecorded()
            except Exception as recovery_error:
                self._merge_stop_failure(f"异常恢复文件写入失败：{recovery_error}")
                self.unsafe_to_close = True

    def _finish_stop(self):
        """后台收尾结束后在主线程恢复待机，并在需要时继续安全关闭窗口。"""
        failure = self._stop_failure
        self.running = self.stopping = self.stop_pending = False
        self.timer.stop()
        self.window._close_blocked = self.unsafe_to_close
        self.window.set_running(False)
        self.statuses = dict.fromkeys(self.statuses, "stopped")
        self.silent_meters = {}
        self.window.set_devices(self.statuses)
        if failure:
            self.last_error = failure
            self.record_event("error", failure, persist=False)
            if hasattr(self.window, "show_failure"):
                self.window.show_failure(failure)
        else:
            self.record_event(
                "info",
                "已停止：已完成的读取记录已保存，未完成的串口请求已取消"
                if self._stop_status == "interrupted"
                else "本次实验记录已完整提交",
                persist=False,
            )
        if self._close_requested:
            self._try_close()

    def fail(self, message):
        LOGGER.error(message)
        self.last_error = message
        if self.running:
            if self.stopping:
                self._note_stop_error(message)
            else:
                self.request_stop(status="error", reason=message)
            if not self.launch_workers and not any(worker.is_alive() for worker in self.workers):
                self.stop(status="error")
        self.record_event("error", message, persist=False)
        if hasattr(self.window, "show_failure"):
            self.window.show_failure(message)
        elif hasattr(self.window, "isVisible") and self.window.isVisible():
            QMessageBox.critical(self.window, "采集或保存异常", message)

    def configure(self):
        """仅待机时修改下一次实验参数；不重新计算已经结束的实验快照。"""
        if self.running:
            return
        from .dialogs import SettingsDialog

        dialog = SettingsDialog(self.settings, self.window)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                try:
                    settings = dialog.values()
                    validate_settings(settings)
                    save_settings(self.data_dir / "settings.json", settings)
                    self.settings = settings
                    self.window.settings = deepcopy(settings)
                    # 停止后的实验与导出快照保持一致；新参数留待下一次开始时使用。
                    self.window.set_devices(self.statuses)
                    self.window.set_running(False)
                    self.record_event("info", "连接、采样时间及报警设置已保存，将用于下次实验")
                except Exception as exc:
                    QMessageBox.warning(self.window, "设置未保存", str(exc))
        finally:
            # 关闭模态窗口只会隐藏；显式销毁以释放Qt信号闭包保留的整套设置控件。
            dialog.deleteLater()

    def capture_snapshot(self):
        # 停止后的界面是最终快照；随后导出不能因时钟继续前进而清空电流/功率。
        """取得独立快照；停止后保留本会话最后结果，防止参数变更改写旧实验。"""
        if not self.running and self.last_snapshot is not None:
            return deepcopy(self.last_snapshot)
        return self.snapshot_at(max(self.clock.now(), self.last_sample_time))

    def export(self):
        """选择TXT或xlsx目标，在独立Qt线程导出冻结快照，拒绝重复并发导出。"""
        if self.engine is None:
            QMessageBox.information(self.window, "保存", "请先开始一次实验，再保存当前页面。")
            return
        if self.exports:
            QMessageBox.information(self.window, "保存", "上一项导出仍在进行，请稍候。")
            return
        destination, selected = QFileDialog.getSaveFileName(
            self.window,
            "保存当前测量结果",
            str(self.data_dir / "exports" / f"测量结果_{self.session_id}.xlsx"),
            "Excel工作簿 (*.xlsx);;UTF-8文本 (*.txt)",
        )
        if not destination:
            return
        if not Path(destination).suffix:
            destination += ".txt" if "*.txt" in selected else ".xlsx"
        task = ExportTask(
            self.store,
            self.capture_snapshot(),
            Path(destination),
            self,
        )
        self.exports.append(task)
        task.completed.connect(
            lambda path: self.record_event("info", f"结果已保存：{path}", persist=False)
        )
        task.failed.connect(lambda message: QMessageBox.warning(self.window, "导出失败", message))
        task.finished.connect(lambda: self.exports.remove(task))
        task.finished.connect(task.deleteLater)
        task.start()

    def history(self):
        """打开历史窗口；采集、收尾、导出或不安全状态下禁止清空正式历史。"""
        from .dialogs import HistoryDialog

        dialog = HistoryDialog(
            self.store,
            self.window,
            allow_clear=not (
                self.running
                or self.stopping
                or self.stop_pending
                or self.exports
                or self.unsafe_to_close
                or (self.recorder and self.recorder.alive)
            ),
        )
        dialog.history_cleared.connect(self.history_cleared)
        dialog.exec()

    def history_cleared(self):
        # 清空后的停止快照不再能被重新导出为已删除的实验。
        """清除已删除会话的内存引用，避免主页面再次导出已不存在的实验。"""
        self.engine = self.recorder = self.clock = self.pulse = None
        self.session_id = self.last_snapshot = None
        self.pending_voltage.clear()
        self.active_alarms.clear()
        self.window.reset_session(self.settings["mode"])
        self.window.set_running(False)
        self.record_event("info", "历史记录已清空", persist=False)

    def shutdown(self):
        self.stop()
        # 导出线程不接触串口，等待落盘后才销毁Qt对象。
        for task in list(self.exports):
            task.wait()

    def request_shutdown(self):
        """把窗口关闭转换为异步停止意图，等待读取和导出结束再允许销毁窗口。"""
        self._close_requested = True
        if self.running:
            self.request_stop()
        self.window._close_blocked = self.running or self.unsafe_to_close or bool(self.exports)
        if self.exports and not self.running:
            QTimer.singleShot(50, self._try_close)

    def _try_close(self):
        """只有采集、导出和安全保存条件全部满足时才安排真正关闭。"""
        if self.running or self.unsafe_to_close:
            return
        if self.exports:
            QTimer.singleShot(50, self._try_close)
            return
        self.window._close_blocked = False
        QTimer.singleShot(0, self.window.close)
