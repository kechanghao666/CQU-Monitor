"""非模态报警通知：当前状态、确认消音与恢复后的重新提醒相互独立。"""

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QTextEdit, QVBoxLayout

LOGGER = logging.getLogger(__name__)


class AlarmSound:
    """Windows异步播放随软件附带的提示音，不在界面线程等待声音结束。"""

    path = Path(__file__).resolve().parent / "assets" / "alarm.wav"

    def play(self):
        import winsound

        winsound.PlaySound(
            str(self.path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        )

    def stop(self):
        import winsound

        winsound.PlaySound(None, 0)


class AlarmNotifier(QObject):
    """只保留当前报警；一个窗口合并显示，已确认报警保持可查但不重复打扰。"""

    def __init__(self, window, button, sound=None):
        super().__init__(window)
        self.window, self.button = window, button
        self.sound = sound or AlarmSound()
        self.active = {}
        self.acknowledged = set()
        self.announced = set()
        self.stopped = False
        self.sound_error = False
        self.sound_active = False
        self.dialog = QDialog(window)
        self.dialog.setWindowTitle("报警提示")
        self.dialog.setWindowModality(Qt.WindowModality.NonModal)
        self.dialog.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.dialog.resize(570, 320)
        layout = QVBoxLayout(self.dialog)
        self.hint = QLabel("请检查设备和测量数值")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)
        confirm = QPushButton("确认")
        confirm.clicked.connect(self.acknowledge)
        layout.addWidget(confirm)
        # 标题栏关闭也视为确认；只是消音，不能修改故障判定或测量数据。
        self.dialog.finished.connect(self.acknowledge)
        self.button.clicked.connect(self.show_current)
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self._play)
        self.reset()

    def _play(self):
        try:
            self.sound.play()
            self.sound_active = True
        except Exception:
            LOGGER.exception("报警提示音播放失败")
            self.sound_error = True
            self._refresh()
            self.timer.stop()

    def _silence(self):
        self.timer.stop()
        # 正常界面每秒刷新多次；没有播放过声音时不反复调用系统音频接口。
        if not self.sound_active:
            return
        try:
            self.sound.stop()
        except Exception:
            LOGGER.exception("停止报警提示音失败")
        finally:
            self.sound_active = False

    def _refresh(self):
        count = len(self.active)
        self.button.setText(f"{'报警记录' if self.stopped else '报警'} {count}")
        self.button.setEnabled(bool(count))
        self.button.setStyleSheet("color:#b62f35;border-color:#d87373;" if count else "")
        details = "\n\n".join(a["message"] for a in self.active.values())
        if self.sound_error:
            details += "\n\n声音播放失败，请检查系统音频。"
        if self.details.toPlainText() != details:
            # 数值刷新时保留阅读位置，避免多项报警把用户滚动的位置反复拉回顶部。
            scroll = self.details.verticalScrollBar().value()
            self.details.setPlainText(details)
            self.details.verticalScrollBar().setValue(scroll)

    def update(self, alarms, notify=True):
        # 兼容历史简化快照；正常采集始终提供稳定的报警 key。
        current = {alarm.get("key", alarm["message"]): dict(alarm) for alarm in alarms}
        # 等待同步变为控制器断线属于新的故障，不能沿用之前的确认状态。
        changed = {
            key
            for key in current.keys() & self.active.keys()
            if current[key].get("condition") != self.active[key].get("condition")
        }
        self.announced.difference_update(changed)
        self.acknowledged.difference_update(changed)
        self.active = current
        self.acknowledged.intersection_update(current)
        self.announced.intersection_update(current)
        pending = current.keys() - self.announced - self.acknowledged
        self._refresh()
        if not current:
            self._silence()
            self.dialog.hide()
        elif not (current.keys() - self.acknowledged):
            self._silence()
        elif pending and notify and self.window.isVisible():
            self.stopped = False
            # 真正显示后才记作已通知，避免窗口隐藏期间产生的报警漏弹。
            self.announced.update(current)
            self.show_current()
            self.timer.start()
            self._play()

    def show_current(self):
        if self.active:
            self._refresh()
            self.dialog.show()
            self.dialog.raise_()
            self.dialog.activateWindow()

    def acknowledge(self, *_):
        self.acknowledged.update(self.active)
        self._silence()
        self.dialog.hide()

    def stop(self):
        """停止阶段消音并保留最后报警，供待机时通过报警记录按钮查看。"""
        self.stopped = True
        self._silence()
        self.dialog.hide()
        self._refresh()

    def reset(self):
        self.active.clear()
        self.acknowledged.clear()
        self.announced.clear()
        self.stopped = False
        self.sound_error = False
        self._silence()
        self.dialog.hide()
        self._refresh()
