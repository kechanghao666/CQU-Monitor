"""串口在独立进程内工作：驱动失去响应时可取消，不阻塞界面或写库。"""

import multiprocessing as mp
import time

from .workers import Producer


class _PipeSink:
    """子进程侧的结果出口；每个完整请求作为一条IPC消息交给父进程。"""

    def __init__(self, connection):
        self.connection = connection

    def put(self, item, timeout=None):
        # 单个已完成请求作为一条管道消息传递；进程不访问数据库或Qt。
        self.connection.send(("record", item))


def _device_main(connection, worker_type, args, clock, stop_event, schedule):
    """构造只读设备循环，退出时发送保留尾包及最终失败原因；不访问Qt或数据库。"""
    try:
        worker = worker_type(*args, _PipeSink(connection), clock, stop_event)
        worker.schedule_control = schedule
        worker.run()
        for item in worker.pending:
            connection.send(("record", item))
        connection.send(("done", worker.failure))
    except BaseException as exc:
        try:
            connection.send(("done", f"串口进程异常：{type(exc).__name__}: {exc}"))
        except (OSError, EOFError):
            pass
    finally:
        connection.close()


def runtime_probe(connection):
    """仅核验打包后的进程启动与IPC，不打开串口，不产生测量值。"""
    connection.send("ready")
    connection.close()


class IsolatedProducer(Producer):
    """用转发线程管理串口子进程；驱动阻塞时可请求取消，Qt主线程不等待驱动。"""

    def __init__(self, worker_type, args, output, clock, stop_event, *, name, stop_grace=3):
        super().__init__(output, clock, stop_event, name)
        self.worker_type, self.args = worker_type, args
        self.stop_grace = stop_grace
        self.context = mp.get_context("spawn")
        self.child_stop = self.context.Event()
        self.schedule = self.context.Value("d", 0)
        self.process = None
        self.forced_stop = False

    @property
    def schedule_origin(self):
        return self.schedule.value

    @schedule_origin.setter
    def schedule_origin(self, value):
        self.schedule.value = value

    def run(self):
        """转发完整消息并处理停止宽限；已完成包继续排空，未完成硬件请求不补数。"""
        receive, send = self.context.Pipe(duplex=False)
        self.process = self.context.Process(
            target=_device_main,
            args=(send, self.worker_type, self.args, self.clock, self.child_stop, self.schedule),
            name=self.name,
        )
        deadline = None
        done = False
        try:
            self.process.start()
            send.close()
            while True:
                if self.stop_event.is_set() and deadline is None:
                    self.child_stop.set()
                    deadline = time.monotonic() + self.stop_grace
                if receive.poll(0.02):
                    try:
                        kind, payload = receive.recv()
                    except EOFError:
                        break
                    if kind == "record":
                        self.publish(payload)
                    elif kind == "done":
                        done = True
                        self.failure = self.failure or payload
                if (
                    deadline is not None
                    and time.monotonic() >= deadline
                    and self.process.is_alive()
                ):
                    # 已传回主进程的完整消息仍继续排空；未完成的硬件请求不制造结果。
                    self.forced_stop = True
                    self.process.terminate()
                    self.process.join(0.5)
                    if self.process.is_alive():
                        self.process.kill()
                        self.process.join(0.5)
                if not self.process.is_alive() and not receive.poll():
                    break
            self.process.join(0.1)
            if not done and not self.stop_event.is_set():
                self.failure = self.failure or f"{self.name}串口进程意外退出"
        except (EOFError, OSError) as exc:
            if isinstance(exc, EOFError) or getattr(exc, "winerror", None) in (109, 232, 233):
                if not done and not self.stop_event.is_set():
                    self.failure = self.failure or f"{self.name}串口进程意外断开"
            else:
                self.failure = self.failure or f"{self.name}串口隔离失败：{exc}"
        except Exception as exc:
            self.failure = self.failure or f"{self.name}串口隔离失败：{exc}"
        finally:
            self.child_stop.set()
            if self.process.pid is not None and self.process.is_alive():
                self.process.terminate()
                self.process.join(0.5)
            receive.close()
            send.close()
            if self.failure:
                self.stop_event.set()
