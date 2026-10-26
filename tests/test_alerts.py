"""验证报警通知不阻塞采集、持续报警去重、确认消音与恢复后重报。"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QPushButton, QWidget


class Sound:
    def __init__(self):
        self.plays = self.stops = 0

    def play(self):
        self.plays += 1

    def stop(self):
        self.stops += 1


def notifier(qtbot):
    from cqu_monitor.alerts import AlarmNotifier

    window = QWidget()
    qtbot.addWidget(window)
    window.show()
    sound = Sound()
    notice = AlarmNotifier(window, QPushButton(window), sound=sound)
    return notice, sound


def alarm(key="current_high", message="电流高于上限"):
    return dict(key=key, message=message, level="warning")


def test_popup_is_nonmodal_deduplicates_and_does_not_block_qt(qtbot):
    notice, sound = notifier(qtbot)
    notice.update([alarm()])
    assert notice.dialog.isVisible() and not notice.dialog.isModal()
    assert notice.dialog.windowModality() == Qt.WindowModality.NonModal
    assert sound.plays == 1 and notice.timer.isActive()
    heartbeats = []
    QTimer.singleShot(5, lambda: heartbeats.append(1))
    for _ in range(8):
        notice.update([alarm()])
    qtbot.waitUntil(lambda: bool(heartbeats))
    assert sound.plays == 1
    notice.acknowledge()
    assert not notice.timer.isActive() and not notice.dialog.isVisible()
    notice.update([alarm()])
    assert sound.plays == 1 and not notice.dialog.isVisible()
    assert notice.button.isEnabled()


def test_new_alarm_merges_recovery_rearms_and_close_mutes(qtbot):
    notice, sound = notifier(qtbot)
    notice.update([alarm()])
    notice.dialog.close()
    assert not notice.timer.isActive()
    notice.update([alarm(), alarm("cell_1_high", "1号小室电压偏高")])
    assert sound.plays == 2
    assert "1号小室" in notice.details.toPlainText()
    assert "电流高于上限" in notice.details.toPlainText()
    notice.update([])
    assert not notice.dialog.isVisible() and not notice.timer.isActive()
    notice.update([alarm()])
    assert sound.plays == 3
    notice.stop()
    assert not notice.dialog.isVisible() and not notice.timer.isActive()
    notice.reset()
    assert not notice.button.isEnabled() and not notice.active


def test_sound_error_is_visible_and_popup_survives(qtbot):
    notice, sound = notifier(qtbot)

    def broken():
        raise RuntimeError("audio unavailable")

    sound.play = broken
    notice.update([alarm()])
    assert notice.dialog.isVisible()
    assert notice.hint.text() == "请检查设备和测量数值"
    assert "声音播放失败" in notice.details.toPlainText()
    notice.acknowledge()


def test_exact_hint_and_confirm_button_mute_without_interrupting_updates(qtbot):
    notice, sound = notifier(qtbot)
    notice.update([alarm()])
    assert notice.hint.text() == "请检查设备和测量数值"
    buttons = notice.dialog.findChildren(QPushButton)
    assert [button.text() for button in buttons] == ["确认"]
    qtbot.mouseClick(buttons[0], Qt.MouseButton.LeftButton)
    assert sound.stops == 1 and not notice.timer.isActive()
    assert not notice.dialog.isVisible()
    notice.update([alarm(message="电流高于上限，当前140")])
    assert sound.plays == 1
    assert "140" in notice.details.toPlainText()
    notice.stop()
    notice.show_current()
    assert notice.hint.text() == "请检查设备和测量数值"
    notice.reset()
    notice.update([alarm("acquisition_failure", "保存失败")])
    assert notice.hint.text() == "请检查设备和测量数值"


def test_hidden_alarm_is_announced_when_window_becomes_visible(qtbot):
    notice, sound = notifier(qtbot)
    notice.window.hide()
    notice.update([alarm()])
    assert sound.plays == 0 and not notice.dialog.isVisible()
    notice.window.show()
    notice.update([alarm()])
    assert sound.plays == 1 and notice.dialog.isVisible()


def test_normal_refresh_does_not_repeatedly_call_audio_system(qtbot):
    notice, sound = notifier(qtbot)
    for _ in range(30):
        notice.update([])
    assert sound.plays == sound.stops == 0
    notice.update([alarm()])
    notice.acknowledge()
    assert sound.stops == 1
    for _ in range(30):
        notice.update([])
    assert sound.stops == 1


def test_controller_fault_change_realerts_after_waiting_was_acknowledged(qtbot):
    notice, sound = notifier(qtbot)
    notice.update([dict(alarm("controller_left"), condition="unconfirmed")])
    notice.acknowledge()
    notice.update([dict(alarm("controller_left"), condition="error")])
    assert sound.plays == 2 and notice.dialog.isVisible()


def test_bundled_sound_is_valid_and_windows_playback_is_async(monkeypatch):
    import wave
    import winsound
    from cqu_monitor.alerts import AlarmSound

    player = AlarmSound()
    with wave.open(str(player.path), "rb") as wav:
        assert wav.getnframes() > 0 and wav.getframerate() >= 8000
    calls = []
    monkeypatch.setattr(winsound, "PlaySound", lambda path, flags: calls.append((path, flags)))
    player.play()
    assert calls[-1][1] & winsound.SND_ASYNC
    assert calls[-1][1] & winsound.SND_FILENAME
    player.stop()
    assert calls[-1][0] is None
