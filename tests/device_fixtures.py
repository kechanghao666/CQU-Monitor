"""仅用于开发验证的仪表/控制器寄存器夹具，不访问真实端口，不进入发行程序。"""

import math
import struct
import threading
import time

from cqu_monitor.workers import SerialProducer
from cqu_monitor.pulse_workers import PulseBusProducer


class TrialClock:
    def __init__(self, speed=1):
        self.speed, self.offset = speed, 0.0
        self.origin = time.monotonic()
        self.sync_origin = 0.0
        self.lock = threading.RLock()

    def now(self):
        with self.lock:
            return self.offset + (time.monotonic() - self.origin) * self.speed

    def set_speed(self, speed):
        # 倍速只存在于测试工具，换速时保持实验时钟连续，不制造时间跳变。
        with self.lock:
            self.offset = self.now()
            self.origin, self.speed = time.monotonic(), speed


class Reply:
    def __init__(self, registers=(), error=False):
        self.registers, self.error = list(registers), error

    def isError(self):
        return self.error

    def __str__(self):
        return "模拟信号注入的电流掉线事件"


class SignalClient:
    """返回原仪表寄存器形状，继续使用生产线程和正式解码器处理响应。"""

    def __init__(self, role, clock, counts):
        self.role, self.clock, self.counts = role, clock, counts

    def connect(self):
        return True

    def close(self):
        pass

    def read_holding_registers(self, address, count, unit):
        expected = (0x56, 1, 1) if self.role == "current" else (0x2A, 4, 1)
        if (address, count, unit) != expected:
            raise ValueError(f"测试捕获不符合原协议的请求：{address}/{count}/{unit}")
        self.counts[self.role] += 1
        elapsed = self.clock.now()
        if self.role == "current":
            # 第25至28实验秒中断，验证记录错误、功率暂不可用和随后恢复。
            if 25 <= elapsed < 28:
                return Reply(error=True)
            return Reply([round(4800 + 200 * math.sin(elapsed / 20))])
        slot = int(max(0, elapsed - self.clock.sync_origin) // 6) % 15
        cell = slot + (16 if self.role == "voltage_right" else 1)
        value = 1.7 + cell * 0.005 + 0.04 * math.sin(elapsed / 20 + cell / 3)
        first, second = struct.unpack(">HH", struct.pack(">f", value))
        return Reply([first, second, 0, 0])


def position(t, group):
    elapsed = t - (1.3 if group == "right" else 0)
    if elapsed < 0:
        return 0, 0, 0, 0
    return int(elapsed // 7) % 15 + 1, elapsed % 7, int(elapsed // 105) + 1, elapsed


class FixtureClient:
    def __init__(self, role, clock):
        self.role, self.clock = role, clock

    def connect(self):
        return True

    def close(self):
        pass

    def read_holding_registers(self, address, count, unit):
        assert unit == 1
        t = self.clock.now()
        if self.role in ("left", "right"):
            if (address, count) == (11, 4):
                return Reply([600, 1, 1, 15])
            assert (address, count) == (22, 6)
            channel, phase, cycle, elapsed = position(t, self.role)
            if self.role == "left" and 44 <= t < 46.5:
                return Reply(error=True)
            if not channel:
                return Reply([0, 0, 0, 0, 0, 0])
            if phase >= 6:
                return Reply([channel, 0, channel, 1, cycle, 0])
            remaining = round((6 - phase) * 100)
            if self.role == "left" and 70 <= t < 72.5:
                remaining = 600
            return Reply([channel, remaining, 0, 0, cycle, 0])
        if self.role == "current":
            assert (address, count) == (0x56, 1)
            return Reply(error=True) if 25 <= t < 28 else Reply([4800])
        assert (address, count) == (0x2A, 4)
        group = "left" if self.role == "voltage_left" else "right"
        channel, phase, cycle, elapsed = position(t, group)
        cell = channel + (15 if group == "right" else 0)
        value = 1.7 + cell * 0.005 + 0.01 * math.sin(t / 20)
        high, low = struct.unpack(">HH", struct.pack(">f", value))
        return Reply([high, low, 0, 0])


class FasterWait:
    def wait_until(self, deadline):
        while not self.stop_event.is_set():
            remaining = deadline - self.clock.now()
            if remaining <= 0:
                return False
            if self.stop_event.wait(max(0.001, remaining / self.clock.speed)):
                return True
        return True


class FixtureMeter(FasterWait, SerialProducer):
    def __init__(self, role, parameters, output, clock, stop_event):
        super().__init__(
            role, parameters, output, clock, stop_event, lambda **kw: FixtureClient(role, clock)
        )


class FixtureBus(FasterWait, PulseBusProducer):
    def __init__(self, groups, output, clock, stop_event):
        group = next(iter(groups))
        super().__init__(
            groups, output, clock, stop_event, lambda **kw: FixtureClient(group, clock)
        )
