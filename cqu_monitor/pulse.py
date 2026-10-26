"""控制器只读观测与电压请求的保守时间夹持，不使用本地计时回退。"""

from collections import deque
import math

from .config import validate_timing


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _json_safe(value):
    # 异常响应也要留存；非有限时刻不能污染严格 JSON 导出。
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _registers(values, count):
    return (
        isinstance(values, (list, tuple))
        and len(values) == count
        and all(type(value) is int and 0 <= value <= 65535 for value in values)
    )


class PulseTracker:
    """分别保存两组有界控制观测，用递减倒计时形成保守的接通时间证据。"""

    MAX_AGE = 0.6
    MAX_SPAN = 0.2
    AFTER_WAIT = 1.0
    STARTUP_GRACE = 3.0

    def __init__(self, profiles, settings=None):
        self.width, self.valid_start, self.valid_end = validate_timing(settings or {})
        self.profiles = {group: dict(profile) for group, profile in profiles.items()}
        self.history = {group: deque(maxlen=150) for group in ("left", "right")}
        self._sequence = dict.fromkeys(self.history, 0)
        self._last_finished = dict.fromkeys(self.history, None)
        self._unconfirmed_since = dict.fromkeys(self.history, None)

    def ingest(self, packet):
        """校验并保存一次原始观测；错误和参数不符也生成可导出的拒绝原因。"""
        group = packet["group"]
        if group not in self.history:
            raise ValueError("控制器组必须为 left 或 right")
        self._sequence[group] += 1
        started, finished = packet.get("started"), packet.get("finished")
        parameters, registers = packet.get("parameters"), packet.get("registers")
        observation = {
            "group": group,
            "sequence": self._sequence[group],
            "started": _json_safe(started),
            "finished": _json_safe(finished),
            "raw_parameters": _json_safe(parameters),
            "raw_registers": _json_safe(registers),
            "channel": None,
            "remaining": None,
            "width": None,
            "outputs": None,
            "cycle": None,
            "interval_remaining": None,
            "cycle_remaining": None,
            "stage": "invalid",
            "status": "invalid_registers",
            "error": str(packet["error"]) if packet.get("error") else None,
            "valid": False,
        }

        def reject(status, reason):
            observation.update(status=status, reason=reason)
            self.history[group].append(observation)
            self._unconfirmed_since[group] = None
            return observation

        previous_finished = self._last_finished[group]
        if not (_finite(started) and _finite(finished) and started <= finished):
            return reject("invalid_time", "控制器读取时刻无效")
        if previous_finished is not None and started < previous_finished:
            return reject("invalid_time", "控制器观测时间倒退或重叠")
        self._last_finished[group] = finished
        if observation["error"]:
            return reject("read_error", "控制器读取失败：" + observation["error"])
        if not _registers(parameters, 4):
            return reject("invalid_parameters", "控制器参数须为4个16位寄存器")
        observation.update(width=parameters[0] * 0.01, outputs=parameters[3])
        if parameters[0] != round(self.width * 100) or parameters[3] != 15:
            return reject(
                "invalid_parameters",
                f"控制器实际输出{observation['width']:g}秒；须与软件设置{self.width:g}秒一致且路数须为15",
            )
        if not _registers(registers, 6):
            return reject("invalid_registers", "控制器状态须为6个16位寄存器")
        scale = self.profiles.get(group, {}).get("countdown_scale")
        if not _finite(scale) or scale not in (0.01, 0.1, 1):
            return reject("invalid_parameters", "输出倒计时单位须为0.01、0.1或1秒")
        channel, countdown, _, interval_remaining, cycle, cycle_remaining = registers
        remaining = countdown * scale
        # 独立列与raw_registers同时保留，历史导出可直接核对两类等待倒计时。
        observation.update(
            channel=channel,
            remaining=remaining,
            cycle=cycle,
            quantum=scale,
            interval_remaining=interval_remaining,
            cycle_remaining=cycle_remaining,
        )
        if not 0 <= channel <= 15 or remaining > observation["width"]:
            return reject("invalid_registers", "控制器通道或输出倒计时超出范围")
        # 等待阶段可以没有输出，但通信本身仍须及时，不能用正常间隔掩盖慢回包。
        if finished - started > self.MAX_SPAN:
            return reject("slow_read", "控制器读取耗时超过0.2秒")
        if interval_remaining or cycle_remaining or remaining == 0:
            observation["stage"] = (
                "cycle_wait" if cycle_remaining else "interval" if interval_remaining else "stopped"
            )
            reason = (
                "控制器处于周期间隔等待"
                if cycle_remaining
                else "控制器处于通道间隔等待"
                if interval_remaining
                else "控制器输出倒计时为零，等待运行状态"
            )
            return reject("waiting", reason)
        if channel == 0:
            return reject("invalid_registers", "输出期间当前路须为1—15")
        observation.update(valid=True, stage="output", status="output", reason="等待连续倒计时确认")
        self.history[group].append(observation)
        _, confirmation, _ = self._consistent(list(self.history[group])[-2:])
        if confirmation == "valid":
            self._unconfirmed_since[group] = None
        elif self._unconfirmed_since[group] is None:
            self._unconfirmed_since[group] = finished
        return observation

    @staticmethod
    def _start_interval(observation):
        # 两次寄存器请求并非原子快照，因此用整个请求跨度及一个量化单位包住起点。
        """将读请求跨度和倒计时量化误差合并为可能的输出起点区间。"""
        delta = observation["remaining"] - observation["width"]
        quantum = observation["quantum"]
        return observation["started"] + delta - quantum, observation["finished"] + delta + quantum

    def _consistent(self, observations):
        """检查同通道同周期的多次观测是否递减且时间证据能相交。"""
        if len(observations) < 2:
            return None, "unconfirmed", "等待第二次递减倒计时观测"
        first = observations[0]
        low, high = -math.inf, math.inf
        previous = None
        for observation in observations:
            if not observation["valid"]:
                return None, observation["status"], observation["reason"]
            if (observation["channel"], observation["cycle"]) != (first["channel"], first["cycle"]):
                return None, "transition", "电压读取跨越控制器通道或周期切换"
            if previous is not None and observation["remaining"] >= previous["remaining"]:
                return None, "frozen", "输出倒计时冻结、增加或重新开始"
            observed_low, observed_high = self._start_interval(observation)
            low, high = max(low, observed_low), min(high, observed_high)
            if low > high:
                return None, "inconsistent", "输出倒计时与读取时刻不相容"
            previous = observation
        return (low, high), "valid", "控制器连续倒计时已确认"

    @staticmethod
    def _cell(group, observation):
        channel = observation.get("channel") if observation else None
        return channel + (15 if group == "right" else 0) if channel and 1 <= channel <= 15 else None

    def snapshot(self, now):
        """生成此刻各组接通状态；过期、等待或冻结时不延用上一盏可靠指示灯。"""
        result = {}
        for group, history in self.history.items():
            profile = self.profiles.get(group, {})
            latest = history[-1] if history else None
            state = {
                "cell": self._cell(group, latest),
                "channel": latest["channel"] if latest else None,
                "phase": None,
                "stage": latest["stage"] if latest else "unknown",
                "active": False,
                "healthy": False,
                "alarm": True,
                "status": "unconfirmed",
                "reason": "等待控制器状态",
                "remaining": latest["remaining"] if latest else None,
                "width": latest["width"] if latest else None,
                "cycle": latest["cycle"] if latest else None,
                "age": None,
                "port": profile.get("port"),
                "unit": profile.get("unit"),
            }
            result[group] = state
            if latest is None:
                # spawn及首次只读请求需要有限启动时间；已收到的明确错误不享受宽限。
                if _finite(now) and 0 <= now <= self.STARTUP_GRACE:
                    state.update(healthy=True, alarm=False)
                else:
                    state.update(status="stale", reason="启动后超过3秒仍未收到控制器状态")
                continue
            if not latest["valid"] and latest["status"] != "waiting":
                state.update(status=latest["status"], reason=latest["reason"])
                continue
            age = now - latest["finished"] if _finite(now) else None
            state["age"] = age
            if age is None or age < 0 or age > self.MAX_AGE:
                state.update(status="stale", reason="控制器状态已过期")
                continue
            if latest["status"] == "waiting":
                state.update(status="waiting", reason=latest["reason"], healthy=True, alarm=False)
                continue
            interval, status, reason = self._consistent(list(history)[-2:])
            state.update(status=status, reason=reason)
            if interval is None:
                if status in ("unconfirmed", "transition", "waiting"):
                    # 换路后的第一条观测不够确认采样，但正常两次轮询间隔不是故障。
                    since = self._unconfirmed_since[group]
                    if since is not None and now - since > self.MAX_AGE:
                        state.update(
                            status="unconfirmed_timeout",
                            reason="控制器持续无法确认通道和递减倒计时",
                        )
                    else:
                        state.update(healthy=True, alarm=False)
                continue
            state.update(healthy=True, alarm=False)
            low, high = interval
            phase_min, phase_max = now - high, now - low
            state["phase"] = (phase_min + phase_max) / 2
            if phase_min < 0 or phase_max >= latest["width"]:
                # 仍在正常通信，只是当前接通证明已到边界；不延长采样资格或亮灯。
                state.update(status="boundary_wait", reason="输出阶段边界，等待下一次控制状态")
                continue
            state["active"] = True
        return result

    @staticmethod
    def _proof(observation):
        """保留判断所依据的最小原始观测字段，供历史导出核对。"""
        if observation is None:
            return None
        return {
            key: observation[key]
            for key in ("sequence", "started", "finished", "channel", "remaining", "cycle")
        }

    def assess(self, group, started, finished, now, final=False):
        """用读取前后两条独立观测夹持电压请求。

        返回None表示尚可等待后侧证据；返回字典表示已判定。只有整个
        请求的全部可能时刻都在有效窗口，才允许该读数进入小室统计。
        """
        history = list(self.history[group])
        sane_sample = (
            _finite(started) and _finite(finished) and _finite(now) and started <= finished
        )
        before = after = None
        if sane_sample:
            candidates = [o for o in history if _finite(o["finished"]) and o["finished"] <= started]
            before = max(candidates, key=lambda o: (o["finished"], o["sequence"]), default=None)
            # Windows 的单调时钟可能把相邻请求记为同一刻；同一条状态不能同时
            # 充当前、后证据。缺少序号更晚的观测时，继续有界等待下一次读取。
            candidates = [
                o
                for o in history
                if _finite(o["started"])
                and o["started"] >= finished
                and (before is None or o["sequence"] > before["sequence"])
            ]
            after = min(candidates, key=lambda o: (o["started"], o["sequence"]), default=None)
            if after is None and 0 <= now - finished < self.AFTER_WAIT and not final:
                return None
        known = before or after
        result = {
            "valid": False,
            "status": "no_bracket",
            "cell": self._cell(group, known),
            "round": known["cycle"] if known else None,
            "phase": None,
            "phase_min": None,
            "phase_max": None,
            "source": "controller",
            "sample_started": _json_safe(started),
            "sample_finished": _json_safe(finished),
            "before": self._proof(before),
            "after": self._proof(after),
        }
        if not sane_sample:
            result["status"] = "invalid_time"
            return result
        if before is None or after is None:
            return result
        if after["finished"] is None or now < after["finished"]:
            result["status"] = "invalid_time"
            return result
        # 读取前后各0.6秒以内才有资格夹持；历史仍保留，供导出追溯无效原因。
        if (
            started - before["finished"] > self.MAX_AGE
            or after["started"] - finished > self.MAX_AGE
        ):
            result["status"] = "stale"
            return result
        between = [o for o in history if before["sequence"] <= o["sequence"] <= after["sequence"]]
        interval, status, _ = self._consistent(between)
        result["status"] = status
        if interval is None:
            return result
        low, high = interval
        phase_min, phase_max = started - high, finished - low
        result.update(phase_min=phase_min, phase_max=phase_max, phase=(phase_min + phase_max) / 2)
        # 电压请求的全部可能时间必须落入用户选定窗口，边界不确定性一律判无效。
        result["valid"] = phase_min >= self.valid_start and phase_max < self.valid_end
        result["status"] = "valid" if result["valid"] else "outside_window"
        return result
