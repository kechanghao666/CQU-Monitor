"""发行包的无设备启动检查：不生成读数，不打开串口。"""

from datetime import datetime
import json
from pathlib import Path
import sys
import multiprocessing as mp

from PySide6.QtWidgets import QApplication, QLabel

from . import __version__
from .config import default_settings
from .dialogs import HistoryDialog, SettingsDialog
from .engine import Engine
from .storage import SessionStore
from .ui import MainWindow


def run_startup_check(output):
    """验证真实发行依赖、隔离进程入口与空白界面；不打开串口、不生成测量数据。"""
    output = Path(output)
    app = QApplication.instance()
    import wave
    from .alerts import AlarmSound

    with wave.open(str(AlarmSound.path), "rb") as alarm_wave:
        assert alarm_wave.getnframes() > 0, "发行包缺少可播放的报警提示音"
    # Windows冻结程序需能分流spawn入口；不使用串口或测量夹具。
    from .isolated import runtime_probe

    context = mp.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=runtime_probe, args=(send,))
    try:
        process.start()
        send.close()
        assert receive.poll(10) and receive.recv() == "ready", "串口隔离运行环境未能启动"
        process.join(3)
        assert process.exitcode == 0
    finally:
        if process.pid is not None and process.is_alive():
            process.terminate()
            process.join(1)
        receive.close()
        send.close()
    settings = default_settings()
    assert settings["synchronization"] == "controller"
    window = MainWindow(settings)
    assert not window.sync_button.isEnabled()
    assert set(window.controller_labels) == {"left", "right"}
    window.show()
    app.processEvents()
    title = window.findChild(QLabel, "title")
    assert abs(title.mapTo(window, title.rect().center()).x() - window.width() / 2) < 3
    assert all(label.text() == "—" for label in window.metric_values.values())
    assert window.settings_button.text() == "参数设置"
    assert window.export_button.text() == "保存数据"
    assert window.scan_label.text() == "本轮进度 0/30"
    assert window.run_label.text() == "●  待机"
    assert not hasattr(window, "alarm_label")
    assert len(window.cell_indicators) == 30
    assert not any(lamp.property("active") for lamp in window.cell_indicators.values())
    assert window.current_measurement_label.text() == "当前测量：待机"
    assert not hasattr(window, "mode_badge") and not hasattr(window, "completeness_label")
    assert [label.text() for label in window.device_names.values()] == [
        "COM1（电压A）",
        "COM2（电压B）",
        "COM3（电流）",
        "COM4（控制器A）",
        "COM5（控制器B）",
    ]
    assert {label.text() for label in window.device_labels.values()} == {"●  待机"}
    blank_pixels = []
    for table in (window.left_table, window.right_table):
        bottom = table.visualItemRect(table.item(14, 0)).bottom()
        blank_pixels.append(table.viewport().height() - 1 - bottom)
    assert all(abs(gap) <= 2 for gap in blank_pixels)
    window.grab().save(str(output / "数据页面.png"))
    window.tabs.setCurrentIndex(1)
    app.processEvents()
    window.grab().save(str(output / "趋势页面.png"))
    window.chart_search.setText("16")
    assert not window.cell_charts[16].isHidden() and window.cell_charts[1].isHidden()
    window.grab().save(str(output / "曲线筛选.png"))
    chart = window.cell_charts[16]
    chart.open_detail()
    app.processEvents()
    chart.detail_dialog.grab().save(str(output / "曲线放大.png"))
    chart.detail_dialog.close()
    dialog = SettingsDialog(settings, window)
    sync_options = [dialog.sync_combo.itemText(i) for i in range(dialog.sync_combo.count())]
    assert sync_options == ["控制器状态同步", "人工同步（兼容旧接线）"]
    assert not hasattr(dialog, "mode_combo") and not hasattr(dialog, "speed_combo")
    dialog.show()
    app.processEvents()
    dialog.grab().save(str(output / "连接参数.png"))
    dialog.settings_tabs.setCurrentIndex(1)
    app.processEvents()
    assert set(dialog.controller_controls) == {"left", "right"}
    dialog.grab().save(str(output / "控制器参数.png"))
    dialog.settings_tabs.setCurrentIndex(2)
    app.processEvents()
    assert [
        dialog.timing_inputs[key].value() for key in ("step_seconds", "valid_start", "valid_end")
    ] == [6, 2, 4]
    dialog.grab().save(str(output / "采样时间.png"))
    dialog.timing_inputs["step_seconds"].setValue(10)
    dialog.timing_inputs["valid_start"].setValue(3)
    dialog.timing_inputs["valid_end"].setValue(7)
    assert dialog.values()["valid_end"] == 7
    dialog.restore_timing_button.click()
    assert dialog.values()["step_seconds"] == 6
    dialog.close()
    store = SessionStore(output / "empty-check.sqlite3")
    started = datetime.now().astimezone().isoformat()
    sid = store.create_session("hardware", settings, started)
    engine = Engine(settings, sid)
    engine.start(0, started)
    snapshot = engine.snapshot(0)
    store.finish_session(sid, snapshot, started)
    for suffix in (".txt", ".xlsx"):
        store.export_snapshot(snapshot, output / ("空白页检查" + suffix))
    assert not store.query_samples(sid)
    history = HistoryDialog(store, window)
    history.show()
    app.processEvents()
    history_headers = [
        history.session_table.horizontalHeaderItem(i).text()
        for i in range(history.session_table.columnCount())
    ]
    assert history_headers == ["开始时间", "结束时间", "状态", "实验编号"]
    assert history.session_table.item(0, 3).text() == sid
    assert history.session_id == sid
    assert history.clear_button.text() == "清空历史记录"
    assert history.clear_button.isEnabled()
    history.grab().save(str(output / "历史记录.png"))
    history.close()
    window.close()
    app.processEvents()
    report = dict(
        passed=True,
        alarm_sound_asset_verified=True,
        version=__version__,
        frozen=bool(getattr(sys, "frozen", False)),
        checked_at=datetime.now().astimezone().isoformat(),
        mode="hardware",
        synchronization_source="controller",
        controller_profiles=settings["controllers"],
        samples_generated=0,
        ports_opened=0,
        cell_count=30,
        chart_count=33,
        search_and_enlarge=True,
        table_tail_blank_pixels=blank_pixels,
        device_captions=[label.text() for label in window.device_names.values()],
        settings_caption=window.settings_button.text(),
        save_caption=window.export_button.text(),
        progress_caption=window.scan_label.text(),
        standby_caption=window.run_label.text(),
        fault_banner_removed=True,
        connection_card_count=len(window.device_labels),
        split_stop_phases=True,
        history_headers=history_headers,
        synchronization_options=sync_options,
        indicator_count=len(window.cell_indicators),
        lit_indicators=0,
        current_measurement_caption=window.current_measurement_label.text(),
        real_hardware_tested=False,
        adjustable_timing=True,
        timing_defaults=[6, 2, 4],
        history_clear_available=True,
        isolated_process_runtime=True,
    )
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
