"""设备侧只读循环与结果协议。

生产环境在isolated.py创建的子进程内调用设备run；父进程只转发完整包，
Qt主线程计算与显示，后台记录器写库。三个层次不能相互直接调用UI或串口。
"""

import math
from queue import Full
import struct
import threading
import time

from pymodbus.client.sync import ModbusSerialClient


def decode_voltage(registers):
    """按原仪表协议把前两个16位寄存器拼成大端浮点；拒绝缺项和非有限数。"""
    if len(registers) != 4 or any(not isinstance(v, int) or not 0 <= v <= 65535 for v in registers):
        raise ValueError("电压响应必须包含4个16位寄存器")
    value = struct.unpack(">f", struct.pack(">HH", *registers[:2]))[0]
    if not math.isfinite(value):
        raise ValueError("电压响应不是有限数值")
    return value


def decode_current(registers):
    """将0—10000原始码换算为0—250A；超出协议量程时明确报错。"""
    if len(registers) != 1 or not isinstance(registers[0], int) or not 0 <= registers[0] <= 10000:
        raise ValueError("电流原始值须处于0—10000；请核对量程和型号")
    return registers[0] * 250.0 / 10000


def next_deadline(previous, now, interval):
    # 以原时间轴推进；电脑忙时跳过错失采样，不连续补读造成假采样密度。
    """按原采样时间轴寻找下一刻，跳过错失周期，禁止补读伪造采样密度。"""
    return previous + max(1, math.floor((now - previous) / interval) + 1) * interval


class ExperimentClock:
    """实验单调时钟；用于采样先后关系，墙钟仅用于显示和导出。"""

    def __init__(self):
        self.origin = time.monotonic()

    def now(self):
        return time.monotonic() - self.origin


class Producer(threading.Thread):
    """统一结果发布与停止协议；生产环境由隔离进程运行具体设备循环。"""

    def __init__(self, output, clock, stop_event, name):
        super().__init__(name=name, daemon=False)
        self.output, self.clock, self.stop_event = output, clock, stop_event
        self.failure = None
        self.pending = []

    def publish(self, item):
        """向有界结果队列发布完整包；队列满则保留尾包并通知停止。"""
        try:
            self.output.put(item, timeout=1)
        except Full:
            # 保留已经取得但未进入主队列的结果，控制器停止后仍能接纳并落盘。
            self.pending.append(item)
            self.failure = "读取结果队列已满，采集已停止，防止继续产生无法保存的数据"
            self.stop_event.set()

    def wait_until(self, deadline):
        # Windows等待可能在单调时钟跨过目标刻度前返回；必须再次检查截止时间，
        # 否则“还差几毫秒”的一次早醒会立刻多读，破坏每秒一次的采样规则。
        """可取消地等到采样截止时刻；早醒时复查，避免同一秒额外读取。"""
        while not self.stop_event.is_set():
            remaining = deadline - self.clock.now()
            if remaining <= 0:
                return False
            if self.stop_event.wait(max(0.001, remaining)):
                return True
        return True


class SerialProducer(Producer):
    """单个仪表的只读采样循环；串口客户端只由本设备循环拥有和关闭。"""

    def __init__(
        self, role, parameters, output, clock, stop_event, client_factory=ModbusSerialClient
    ):
        super().__init__(output, clock, stop_event, f"reader-{role}")
        self.role, self.parameters, self.client_factory = role, parameters.copy(), client_factory
        self.schedule_origin = 0.0

    def close_client(self, client):
        """关闭本仪表客户端；关闭失败进入可追溯错误并通知全局停止。"""
        try:
            client.close()
        except Exception as exc:
            self.failure = f"{self.role}关闭串口失败：{exc}"
            self.stop_event.set()

    def run(self):
        """逐次读取并发布成功或失败尝试，持续按1Hz推进，不发送设备写命令。"""
        client = None
        deadline = 0.5
        try:
            while not self.wait_until(deadline):
                started = self.clock.now()
                value, error = None, None
                try:
                    if client is None:
                        self.publish({"kind": "device", "role": self.role, "state": "connecting"})
                        client = self.client_factory(
                            method="rtu",
                            port=self.parameters["port"],
                            baudrate=self.parameters["baudrate"],
                            bytesize=8,
                            parity="N",
                            stopbits=1,
                            timeout=self.parameters["timeout"],
                            retries=0,
                            retry_on_empty=False,
                            strict=False,
                        )
                        if not client.connect():
                            raise OSError("无法打开串口，请检查连接或串口是否被其他软件占用")
                    started = self.clock.now()
                    response = client.read_holding_registers(
                        0x0056 if self.role == "current" else 0x002A,
                        1 if self.role == "current" else 4,
                        unit=self.parameters["unit"],
                    )
                    finished = self.clock.now()
                    if response.isError():
                        raise OSError(f"仪表响应异常：{response}")
                    value = (decode_current if self.role == "current" else decode_voltage)(
                        response.registers
                    )
                except Exception as exc:
                    finished = self.clock.now()
                    error = f"{type(exc).__name__}: {exc}"
                    if client is not None:
                        self.close_client(client)
                        client = None
                self.publish(
                    {
                        "kind": "reading",
                        "role": self.role,
                        "value": value,
                        "started": started,
                        "finished": finished,
                        "error": error,
                    }
                )
                self.publish(
                    {
                        "kind": "device",
                        "role": self.role,
                        "state": "disconnected" if error else "connected",
                    }
                )
                origin = (
                    self.schedule_control.value
                    if hasattr(self, "schedule_control")
                    else self.schedule_origin
                )
                deadline = next_deadline(origin + 0.5, finished, 1)
        except Exception as exc:
            self.failure = f"{self.role}采集线程停止：{exc}"
            self.stop_event.set()
        finally:
            if client is not None:
                self.close_client(client)
