"""30 小室测量的时序与统计核心；所有调用由控制器在同一线程串行执行。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real

from .config import validate_timing


ROLES = ("voltage_left", "voltage_right", "current")
VOLTAGE_MAX_AGE = 96.0
CURRENT_MAX_AGE = 2.5


def _finite(value: object) -> float | None:
    """把非法数值统一变为缺测，保证输出可以严格写入 JSON。"""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass
class _Mean:
    """常数空间的递推算术均值；每次add代表一个有效统计样本，不是连续积分。"""

    count: int = 0
    value: float | None = None

    def add(self, value: float) -> None:
        # 加权递推无需保存历史，也避免长期累加和随运行时间不断增长。
        """计入一个有效值，按已有数量递推均值，避免保存全部历史或长期累加溢出。"""
        self.count += 1
        if self.count == 1:
            self.value = value
        else:
            self.value = self.value * ((self.count - 1) / self.count) + value / self.count


class Engine:
    """纯计算接口；不导入 Qt、不访问硬件、不负责持久化。"""

    def __init__(self, config: dict, session_id: str):
        if not isinstance(config, dict):
            raise ValueError("config 必须为字典")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id 必须为非空字符串")
        self.config = dict(config)
        self.session_id = session_id
        self.mode = config.get("mode", "hardware")
        self.synchronization = config.get("synchronization", "manual")
        if self.synchronization not in ("manual", "controller"):
            raise ValueError("同步方式无效")
        if self.mode != "hardware":
            raise ValueError("正式版仅接纳真实采集会话")
        self.step_seconds, self.valid_start, self.valid_end = validate_timing(config)
        # 一轮15路加一路裕量；默认仍为96秒，长脉冲不会被固定旧时限误报。
        self.voltage_max_age = 16 * self.step_seconds
        self.group_max_age = dict.fromkeys(("left", "right"), self.voltage_max_age)
        self.thresholds = {}
        for prefix in ("voltage", "current"):
            for bound in ("low", "high"):
                key = f"{prefix}_{bound}"
                value = config.get(key)
                parsed = _finite(value)
                if value is not None and parsed is None:
                    raise ValueError(f"{key} 必须为空或有限数值")
                self.thresholds[key] = parsed
            low, high = self.thresholds[f"{prefix}_low"], self.thresholds[f"{prefix}_high"]
            if low is not None and high is not None and low > high:
                raise ValueError(f"{prefix} 下限不能高于上限")
        self._reset()

    def _reset(self) -> None:
        """重置本次实验统计；已保存的其他会话由存储层独立保留。"""
        self._start = None
        self._start_wall = None
        self._sync = None
        self._sync_version = 0
        self._cells = [dict(latest=None, mean=_Mean(), updated=None, wall=None) for _ in range(30)]
        self._role_finished = dict.fromkeys(ROLES)
        self._healthy = dict.fromkeys(ROLES, False)
        self._responsive = dict.fromkeys(ROLES, True)
        self._current = None
        self._current_updated = None
        self._current_mean = _Mean()
        self._total_mean = _Mean()
        self._power_mean = _Mean()
        self._last_tick = -1
        self._controller_health = dict.fromkeys(("left", "right"), False)

    def set_controller_health(self, group, healthy):
        """更新指定组的同步可靠性，仅影响质量判断，不删除已保存的最新电压。"""
        if group not in self._controller_health:
            raise ValueError("控制器分组无效")
        self._controller_health[group] = bool(healthy)

    def set_meter_responsive(self, role, responsive):
        """独立监测驱动无回包；不伪造读数或覆盖上一次真实请求的成功/失败。"""
        if role not in ROLES:
            raise ValueError("仪表角色无效")
        self._responsive[role] = bool(responsive)

    def set_controller_intervals(self, group, interval, cycle_interval):
        """把实际路间/周期间隔纳入本组旧值判断，左右组互不影响。"""
        if group not in self.group_max_age or any(
            type(value) is not int or not 0 <= value <= 65535
            for value in (interval, cycle_interval)
        ):
            raise ValueError("控制器间隔参数无效")
        # 完整15路含路间及周期间等待，另留一路宽度裕量；两组独立计算。
        self.group_max_age[group] = 16 * self.step_seconds + 15 * interval + cycle_interval

    def _max_age(self, index):
        return self.group_max_age["left" if index < 15 else "right"]

    def _voltage_healthy(self, role):
        group = "left" if role == "voltage_left" else "right"
        return (
            self._healthy[role]
            and self._responsive[role]
            and (self.synchronization == "manual" or self._controller_health[group])
        )

    @staticmethod
    def _clock(value: float) -> float:
        parsed = _finite(value)
        if parsed is None or parsed < 0:
            raise ValueError("单调时钟必须是非负有限数值")
        return parsed

    def _now(self, value: float) -> float:
        now = self._clock(value)
        if self._start is not None and now < self._start:
            raise ValueError("时间不能早于本实验开始")
        return now

    def start(self, start_mono: float, wall_time: str) -> None:
        """以实验单调起点开始新统计；墙钟字符串只用于追溯和显示。"""
        start = self._clock(start_mono)
        self._reset()
        self._start = start
        self._start_wall = str(wall_time)

    def synchronize(self, sync_mono: float) -> None:
        """人工兼容模式重新对齐1/16路；清除当前扫描最新值，保留本实验累计均值。"""
        if self.synchronization == "controller":
            raise ValueError("控制器同步模式不采用人工计时零点")
        if self._start is None:
            raise ValueError("请先开始实验再同步")
        sync = self._now(sync_mono)
        if self._sync is not None and sync < self._sync:
            raise ValueError("同步时间不能倒退")
        self._sync = sync
        self._sync_version += 1
        # 重新对齐后旧扫描不再参与总压；本实验的累计均值仍保留。
        for cell in self._cells:
            cell.update(latest=None, updated=None, wall=None)
        for role in ROLES[:2]:
            self._healthy[role] = False

    def _position(self, mono: float) -> tuple[int | None, int | None, float | None]:
        if self._sync is None or mono < self._sync:
            return None, None, None
        delta = mono - self._sync
        step = int(delta // self.step_seconds)
        return step % 15, step // 15 + 1, delta % self.step_seconds

    def accept(
        self,
        role: str,
        value: float | None,
        started_mono: float,
        finished_mono: float,
        wall_time: str,
        error: str | None = None,
        sync_context: dict | None = None,
    ) -> dict:
        """校验一条仪表尝试并返回完整记录；无效尝试保留原因但不加入有效均值。"""
        if role not in ROLES:
            raise ValueError(f"未知设备角色：{role}")
        if self._start is None:
            raise ValueError("请先开始实验再接收数据")
        number = _finite(value)
        started, finished = _finite(started_mono), _finite(finished_mono)
        previous = self._role_finished[role]
        bad_time = (
            started is None
            or finished is None
            or started < self._start
            or finished < started
            or (previous is not None and started < previous)
        )
        slot, round_number, phase = (None, None, None)
        cell_number = None
        controlled = self.synchronization == "controller" and role != "current"
        if not bad_time and role != "current":
            if controlled:
                if isinstance(sync_context, dict):
                    candidate = sync_context.get("cell")
                    lower = 1 if role == "voltage_left" else 16
                    if type(candidate) is int and lower <= candidate < lower + 15:
                        cell_number = candidate
                        slot = (candidate - 1) % 15
                    round_number = sync_context.get("round")
                    phase = _finite(sync_context.get("phase"))
            else:
                slot, round_number, phase = self._position(started)
                if slot is not None:
                    cell_number = slot + (1 if role == "voltage_left" else 16)
        status = "valid"
        if bad_time:
            status = "invalid_time"
        elif error is not None:
            status = "error"
        elif number is None:
            status = "invalid_value"
        elif role != "current":
            if controlled:
                context = sync_context if isinstance(sync_context, dict) else {}
                start_phase = _finite(context.get("phase_min"))
                end_phase = _finite(context.get("phase_max"))
                # 上下游都检查证据：不接纳属于别的请求、组别或窗口的有效标记。
                proven = (
                    context.get("valid") is True
                    and context.get("source") == "controller"
                    and context.get("sample_started") == started
                    and context.get("sample_finished") == finished
                    and cell_number is not None
                    and start_phase is not None
                    and end_phase is not None
                    and self.valid_start <= start_phase <= end_phase < self.valid_end
                )
                status = (
                    "valid" if proven else str(context.get("status") or "controller_unconfirmed")
                )
                if not proven and status == "valid":
                    status = "invalid_controller_evidence"
            elif slot is None:
                status = "unsynchronized"
            else:
                end_slot, end_round, end_phase = self._position(finished)
                # 必须检查两个端点和绝对步号；只检查相位会漏掉跨整轮请求。
                if (
                    slot != end_slot
                    or round_number != end_round
                    or not self.valid_start <= phase < self.valid_end
                    or not self.valid_start <= end_phase < self.valid_end
                ):
                    status = "out_of_window"
        valid = status == "valid"
        if not bad_time:
            self._role_finished[role] = finished
            self._healthy[role] = error is None and number is not None
            self._responsive[role] = True
        mean = (
            self._current_mean.value
            if role == "current"
            else (self._cells[cell_number - 1]["mean"].value if cell_number is not None else None)
        )
        if valid:
            if role == "current":
                self._current = number
                self._current_updated = finished
                self._current_mean.add(number)
                mean = self._current_mean.value
            else:
                cell = self._cells[cell_number - 1]
                cell.update(latest=number, updated=finished, wall=str(wall_time))
                cell["mean"].add(number)
                mean = cell["mean"].value
        elapsed = (
            finished - self._start if finished is not None and finished >= self._start else None
        )
        return dict(
            session_id=self.session_id,
            role=role,
            cell=cell_number,
            round=round_number,
            phase=phase,
            elapsed=elapsed,
            wall_time=str(wall_time),
            value=number,
            valid=valid,
            status=status,
            error=None if error is None else str(error),
            mean_value=mean,
            sync_version=self._sync_version,
            synchronization_source=self.synchronization,
            sync_evidence=sync_context if controlled else None,
        )

    def _metrics(self, now: float) -> tuple[dict, bool, str]:
        """用每室最新有效值计算部分总压；电流失效时实时电流与功率留空。"""
        measured = [(i, cell) for i, cell in enumerate(self._cells) if cell["latest"] is not None]
        available = [
            (i, c)
            for i, c in measured
            if 0 <= now - c["updated"] <= self._max_age(i)
            and self._voltage_healthy("voltage_left" if i < 15 else "voltage_right")
        ]
        total = None
        count, fresh_count = len(measured), len(available)
        # 已测小室立即参与合计；同一小室只替换最新值，不能把重复读取叠加为新小室。
        if measured:
            try:
                total = _finite(math.fsum(cell["latest"] for _, cell in measured))
            except OverflowError:
                total = None
        quality = (
            "missing"
            if not count
            else "degraded"
            if fresh_count < count or total is None
            else "complete"
            if count == 30
            else "partial"
        )
        coverage = f"已测 {count}/30"
        reason = (
            "等待同步"
            if self._sync is None and self.synchronization == "manual"
            else "尚无有效电压"
            if not count
            else "总电压数值溢出"
            if total is None
            else f"{coverage}；含{count - fresh_count}室旧值或故障设备读数，请核对报警"
            if quality == "degraded"
            else f"{coverage}；当前总压为已测小室合计，功率为对应估算"
            if count < 30
            else "已测30/30；总压为各室最近有效读数之和（轮询非同时采集）"
        )
        current = None
        if (
            self._healthy["current"]
            and self._responsive["current"]
            and self._current_updated is not None
            and 0 <= now - self._current_updated <= CURRENT_MAX_AGE
        ):
            current = self._current
        power = (
            _finite(total * (current / 1000)) if total is not None and current is not None else None
        )
        metrics = dict(
            total_voltage=total,
            avg_total_voltage=self._total_mean.value,
            current=current,
            power_kw=power,
            avg_power_kw=self._power_mean.value,
            measured_count=count,
            available_count=fresh_count,
            coverage=coverage,
            quality=quality,
            reason=reason,
        )
        return metrics, quality == "complete", reason

    def tick(self, now_mono: float, wall_time: str) -> dict | None:
        """每个到达的实验整数秒最多累计一次总压/功率；错失的秒不回填。"""
        now = self._now(now_mono)
        if self._start is None:
            return None
        elapsed = now - self._start
        second = int(elapsed)
        if second <= self._last_tick:
            return None
        self._last_tick = second
        metrics, complete, reason = self._metrics(now)
        # 每个到达的实验整数秒只统计一次，跳秒不补数，快照完全不写统计。
        if metrics["total_voltage"] is not None:
            self._total_mean.add(metrics["total_voltage"])
        if metrics["power_kw"] is not None:
            self._power_mean.add(metrics["power_kw"])
        metrics.update(
            avg_total_voltage=self._total_mean.value, avg_power_kw=self._power_mean.value
        )
        return dict(
            session_id=self.session_id,
            elapsed=elapsed,
            wall_time=str(wall_time),
            **metrics,
            complete=complete,
        )

    def _alarms(self, metrics: dict, cells: list[dict]) -> list[dict]:
        """根据当前质量、设备健康与手动阈值生成报警；超限不删除原始读数。"""
        alarms = []
        if metrics["quality"] == "degraded":
            alarms.append(dict(key="voltage_quality", level="warning", message=metrics["reason"]))
        for role in ROLES:
            if self._role_finished[role] is not None and not self._healthy[role]:
                label = {
                    "voltage_left": "左组电压表",
                    "voltage_right": "右组电压表",
                    "current": "电流表",
                }[role]
                alarms.append(
                    dict(key=f"{role}_offline", level="error", message=f"{label}最近读取失败")
                )
        if (
            self._current_updated is not None
            and metrics["current"] is None
            and self._healthy["current"]
        ):
            alarms.append(
                dict(
                    key="current_stale", level="warning", message="电流读数已过期，实时功率暂不可用"
                )
            )
        for prefix, readings in (
            ("voltage", [(f"cell_{c['cell']}", c["latest"], f"{c['cell']}号小室") for c in cells]),
            ("current", [("current", metrics["current"], "电流")]),
        ):
            for key, value, label in readings:
                if value is None:
                    continue
                for bound, compare, text in (
                    ("low", lambda a, b: a < b, "低于下限"),
                    ("high", lambda a, b: a > b, "高于上限"),
                ):
                    threshold = self.thresholds[f"{prefix}_{bound}"]
                    # 0 是有效阈值，不能用布尔真假判断是否启用。
                    if threshold is not None and compare(value, threshold):
                        alarms.append(
                            dict(
                                key=f"{key}_{bound}",
                                level="warning",
                                message=f"{label}{text} {threshold:g}，当前 {value:g}",
                            )
                        )
        return alarms

    def snapshot(self, now_mono: float) -> dict:
        """生成只读显示快照；快照调用本身不改变样本数或累计均值。"""
        now = self._now(now_mono)
        slot, round_number, phase = self._position(now)
        metrics, _, _ = self._metrics(now)
        cells = []
        for index, cell in enumerate(self._cells):
            age = None if cell["updated"] is None else now - cell["updated"]
            role = "voltage_left" if index < 15 else "voltage_right"
            if cell["latest"] is None:
                status = "missing"
            elif age < 0 or age > self._max_age(index):
                status = "stale"
            elif not self._voltage_healthy(role):
                status = "error"
            else:
                status = "valid"
            cells.append(
                dict(
                    cell=index + 1,
                    latest=cell["latest"],
                    mean=cell["mean"].value,
                    count=cell["mean"].count,
                    updated_at=cell["wall"],
                    age=age,
                    status=status,
                )
            )
        return dict(
            session_id=self.session_id,
            mode=self.mode,
            elapsed=0.0 if self._start is None else now - self._start,
            synchronized=(self._sync is not None and now >= self._sync)
            if self.synchronization == "manual"
            else any(self._controller_health.values()),
            synchronization_source=self.synchronization,
            sync_version=self._sync_version,
            slot=slot,
            round=round_number,
            phase=phase,
            cell_pair=None if slot is None else [slot + 1, slot + 16],
            cells=cells,
            metrics=metrics,
            alarms=self._alarms(metrics, cells),
        )
