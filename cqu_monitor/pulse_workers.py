"""每条物理总线在隔离进程中顺序读取左右控制器，不能并发争抢同一RS485总线。"""

from pymodbus.client.sync import ModbusSerialClient

from .workers import Producer, next_deadline


class PulseBusProducer(Producer):
    """一条RS485物理总线的控制器读取器；共线设备按不同站号顺序访问。"""

    def __init__(self, groups, output, clock, stop_event, client_factory=ModbusSerialClient):
        if not groups:
            raise ValueError("控制器总线至少需要一个组")
        self.groups = {group: dict(profile) for group, profile in groups.items()}
        first = next(iter(self.groups.values()))
        self.parameters = first
        serial_keys = ("port", "baudrate", "parity", "stopbits", "timeout")
        for group, profile in self.groups.items():
            if group not in ("left", "right"):
                raise ValueError("控制器组必须为 left 或 right")
            if any(profile[key] != first[key] for key in serial_keys):
                raise ValueError("同一串口的串行参数必须一致")
            if profile["function"] not in (3, 4) or profile["address_offset"] not in (0, -1):
                raise ValueError("控制器仅支持03/04只读及0/-1地址偏移")
        if len({profile["unit"] for profile in self.groups.values()}) != len(self.groups):
            raise ValueError("同一串口的控制器站号须不同")
        super().__init__(output, clock, stop_event, f"pulse-{first['port']}")
        self.client_factory = client_factory

    def read_group(self, client, group):
        """顺序读取参数和状态，记录包住两次请求的起止时刻，不假设原子快照。"""
        profile = self.groups[group]
        packet = {
            "kind": "controller",
            "group": group,
            "started": self.clock.now(),
            "finished": None,
            "parameters": None,
            "registers": None,
            "error": None,
        }
        try:
            read = (
                client.read_holding_registers
                if profile["function"] == 3
                else client.read_input_registers
            )
            # 起止时刻包住参数和状态两个请求，不把中间读取误认为原子状态。
            for key, address, count in (("parameters", 11, 4), ("registers", 22, 6)):
                response = read(address + profile["address_offset"], count, unit=profile["unit"])
                if response.isError():
                    raise OSError(f"控制器响应异常：{response}")
                packet[key] = list(response.registers)
        except Exception as exc:
            packet["error"] = f"{type(exc).__name__}: {exc}"
        packet["finished"] = self.clock.now()
        return packet

    def close_client(self, client):
        """关闭当前总线客户端；异常交给父进程进入停止和数据收尾。"""
        try:
            client.close()
        except Exception as exc:
            self.failure = f"控制器关闭串口失败：{exc}"
            self.stop_event.set()

    def run(self):
        """轮询总线各组原始寄存器，失败仍发布证据；从不根据本地计时伪造通道。"""
        client = None
        deadline = self.clock.now()
        try:
            while not self.wait_until(deadline):
                for group in self.groups:
                    if self.stop_event.is_set():
                        break
                    error = None
                    if client is None:
                        self.publish(
                            {"kind": "device", "role": f"controller_{group}", "state": "connecting"}
                        )
                        if self.stop_event.is_set():
                            break
                        started = self.clock.now()
                        try:
                            client = self.client_factory(
                                method="rtu",
                                port=self.parameters["port"],
                                baudrate=self.parameters["baudrate"],
                                bytesize=8,
                                parity=self.parameters["parity"],
                                stopbits=self.parameters["stopbits"],
                                timeout=self.parameters["timeout"],
                                retries=0,
                                retry_on_empty=False,
                                strict=False,
                            )
                            if not client.connect():
                                raise OSError("无法打开控制器串口")
                        except Exception as exc:
                            error = f"{type(exc).__name__}: {exc}"
                    if error:
                        packet = {
                            "kind": "controller",
                            "group": group,
                            "started": started,
                            "finished": self.clock.now(),
                            "parameters": None,
                            "registers": None,
                            "error": error,
                        }
                    else:
                        packet = self.read_group(client, group)
                    self.publish(packet)
                    self.publish(
                        {
                            "kind": "device",
                            "role": f"controller_{group}",
                            "state": "disconnected" if packet["error"] else "connected",
                        }
                    )
                    if packet["error"] and client is not None:
                        self.close_client(client)
                        client = None
                deadline = next_deadline(deadline, self.clock.now(), 0.2)
        except Exception as exc:
            self.failure = f"控制器采集线程停止：{exc}"
            self.stop_event.set()
        finally:
            # 线程拥有客户端，退出前关闭；已产出包仍由父类队列及pending交给主线程排空。
            if client is not None:
                self.close_client(client)
