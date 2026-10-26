"""Windows桌面入口。开发版与独立程序共用入口及离线验收。"""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import traceback

from PySide6.QtCore import QLockFile
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from cqu_monitor.config import load_settings
from cqu_monitor.controller import Controller
from cqu_monitor.ui import MainWindow


def main():
    """解析入口与数据目录，持有单实例锁；默认只打开待机界面，点击开始才读串口。"""
    parser = argparse.ArgumentParser(description="重庆大学电解水制氢系统")
    parser.add_argument("--data-dir", type=Path, help="数据目录，默认程序同目录data")
    parser.add_argument(
        "--check-startup", action="store_true", help="验证空白界面及依赖，不连接仪表"
    )
    parser.add_argument("--output", type=Path, help="启动检查输出目录")
    args = parser.parse_args()
    base = (
        Path(sys.executable).parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent.parent
    )
    data_dir = (
        args.output if args.check_startup and args.output else args.data_dir or base / "data"
    ).resolve()
    log_dir = data_dir / "logs"
    # 先建立可报告错误的界面；磁盘不可写时不能在窗口建立之前无声退出。
    app = QApplication(sys.argv[:1])
    app.setApplicationName("重庆大学电解水制氢系统")
    app.setOrganizationName("CQU Lab")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setQuitOnLastWindowClosed(not args.check_startup)
    controller = lock = handler = None
    previous_exception_hook = sys.excepthook
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "application.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        logging.basicConfig(
            level=logging.INFO,
            handlers=[handler],
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        # 同一数据目录只允许一个采集实例，防止另一个进程误标会话中断。
        lock = QLockFile(str(data_dir / "application.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(100):
            QMessageBox.warning(None, "程序已打开", f"此数据目录已有程序在运行：\n{data_dir}")
            return 2
        if args.check_startup:
            from cqu_monitor.startup_check import run_startup_check

            report = run_startup_check(data_dir)
            if sys.stdout:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        settings = load_settings(data_dir / "settings.json")
        # 启动只显示待机界面；用户点击开始后才打开配置的真实串口。
        window = MainWindow(settings)
        controller = Controller(window, settings, data_dir)

        def on_exception(kind, value, tb):
            message = "".join(traceback.format_exception(kind, value, tb))
            logging.getLogger(__name__).error(message)
            controller.fail(f"程序异常，采集已停止。详情见日志：\n{value}")

        sys.excepthook = on_exception
        app.aboutToQuit.connect(controller.shutdown)
        window.show()
        return app.exec()
    except Exception as exc:
        logging.getLogger(__name__).exception("启动或组件检查失败")
        if args.check_startup:
            try:
                (data_dir / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
            except OSError:
                # 自动验收以非零退出码报告失败；坏盘也可能无法留下本地证据。
                logging.getLogger(__name__).exception("启动检查失败信息也无法写入磁盘")
        else:
            QMessageBox.critical(None, "程序未能启动", f"{exc}\n\n请查看：{log_dir}")
        return 1
    finally:
        if controller:
            controller.shutdown()
        sys.excepthook = previous_exception_hook
        if lock:
            lock.unlock()
        if handler:
            logging.getLogger().removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
