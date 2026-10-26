"""真实设置窗口走模态事件循环后必须释放其Qt对象和信号闭包。"""

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer
from PySide6.QtWidgets import QDialog

from cqu_monitor.config import default_settings
from cqu_monitor.controller import Controller
from cqu_monitor.dialogs import SettingsDialog
from cqu_monitor.ui import MainWindow


@pytest.mark.parametrize("action", ["reject", "accept", "close"])
def test_repeated_settings_dialog_exit_releases_all_children(qtbot, tmp_path, monkeypatch, action):
    settings = default_settings()
    window = MainWindow(settings)
    qtbot.addWidget(window)
    c = Controller(window, settings, tmp_path, launch_workers=False)
    before = None
    original_exec = QDialog.exec

    def auto_exit(dialog):
        QTimer.singleShot(0, getattr(dialog, action))
        return original_exec(dialog)

    monkeypatch.setattr(QDialog, "exec", auto_exit)
    for _ in range(3):
        c.configure()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert window.findChildren(SettingsDialog) == []
        # Qt首次进入模态循环会创建焦点等内部对象；比较预热后的平台。
        if before is None:
            before = len(window.findChildren(QObject))
        else:
            assert len(window.findChildren(QObject)) == before
    c.shutdown()
