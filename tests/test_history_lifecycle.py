"""验证真正的历史入口关闭后销毁控件，避免重复查询导致内存累积。"""

from PySide6.QtCore import QCoreApplication, QEvent, QTimer

from cqu_monitor.config import default_settings
from cqu_monitor.controller import Controller
from cqu_monitor.dialogs import HistoryDialog
from cqu_monitor.ui import MainWindow


def test_repeated_controller_history_closes_release_all_dialogs(qtbot, tmp_path, monkeypatch):
    window = MainWindow(default_settings())
    qtbot.addWidget(window)
    controller = Controller(window, window.settings, tmp_path, launch_workers=False)
    original_exec = HistoryDialog.exec

    def close_after_open(dialog):
        QTimer.singleShot(0, dialog.reject)
        return original_exec(dialog)

    monkeypatch.setattr(HistoryDialog, "exec", close_after_open)
    for _ in range(12):
        controller.history()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not window.findChildren(HistoryDialog)
