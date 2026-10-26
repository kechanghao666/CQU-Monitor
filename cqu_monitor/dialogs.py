"""连接参数与历史查询；不创建采集客户端、不改变当前实验。"""

from copy import deepcopy
from datetime import datetime
import re

from serial.tools import list_ports
from PySide6.QtCore import QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QWidget,
    QVBoxLayout,
)

from .ui import ROLES, STYLE, card, number
from .config import device_caption, DEFAULT_PORTS, default_settings, validate_settings
from .pulse_settings import controller_page, controller_values


class SettingsDialog(QDialog):
    """收集下一次实验的仪表、两控制器、有效时窗和手动阈值参数。"""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._original = deepcopy(settings)
        self.setWindowTitle("连接与实验参数")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(760)
        self.resize(820, 650)
        body = QVBoxLayout(self)
        body.setContentsMargins(24, 22, 24, 20)
        body.setSpacing(15)
        title = QLabel("连接与实验参数")
        title.setObjectName("section")
        body.addWidget(title)

        sync_row = QHBoxLayout()
        sync_row.addWidget(QLabel("小室同步来源"))
        self.sync_combo = QComboBox()
        self.sync_combo.addItem("控制器状态同步", "controller")
        self.sync_combo.addItem("人工同步（兼容旧接线）", "manual")
        self.sync_combo.setCurrentIndex(
            max(0, self.sync_combo.findData(settings.get("synchronization", "manual")))
        )
        sync_row.addWidget(self.sync_combo, 1)
        body.addLayout(sync_row)
        self.settings_tabs = QTabWidget()
        meter_page = QWidget()
        meter_body = QVBoxLayout(meter_page)
        meter_body.setContentsMargins(0, 8, 0, 0)
        self.settings_tabs.addTab(meter_page, "仪表与报警")
        controller_tab, self.controller_controls = controller_page(settings)
        self.settings_tabs.addTab(controller_tab, "两组控制器")
        timing_page = QWidget()
        timing_layout = QVBoxLayout(timing_page)
        timing_card = card()
        timing_grid = QGridLayout(timing_card)
        self.timing_inputs = {}
        for row, (key, caption, minimum) in enumerate(
            (
                ("step_seconds", "脉冲输出时长 / 秒", 0.01),
                ("valid_start", "有效采样起始 / 输出后第几秒", 0),
                ("valid_end", "有效采样结束 / 输出后第几秒", 0.01),
            )
        ):
            widget = QDoubleSpinBox()
            widget.setRange(minimum, 99.99)
            widget.setDecimals(2)
            widget.setSingleStep(0.1)
            widget.setValue(settings.get(key, default_settings()[key]))
            self.timing_inputs[key] = widget
            timing_grid.addWidget(QLabel(caption), row, 0)
            timing_grid.addWidget(widget, row, 1)
        timing_layout.addWidget(timing_card)
        self.timing_note = QLabel()
        self.timing_note.setObjectName("subtitle")
        self.timing_note.setWordWrap(True)
        timing_layout.addWidget(self.timing_note)
        self.restore_timing_button = QPushButton("恢复默认：6 秒输出，取第 2～4 秒")
        self.restore_timing_button.clicked.connect(self.restore_timing)
        timing_layout.addWidget(self.restore_timing_button)
        timing_layout.addStretch()
        self.settings_tabs.addTab(timing_page, "采样时间")
        for widget in self.timing_inputs.values():
            widget.valueChanged.connect(self.update_timing_note)
        self.update_timing_note()
        body.addWidget(self.settings_tabs, 1)

        port_card = card()
        ports = QGridLayout(port_card)
        ports.setContentsMargins(15, 14, 15, 14)
        for col, text in enumerate(("采集设备", "串口", "波特率", "站号", "超时 / s")):
            label = QLabel(text)
            label.setStyleSheet("font-weight:600;color:#5e758b;font-size:12px;")
            ports.addWidget(label, 0, col)
        self.port_controls = {}
        self.device_names = {}
        for row, (role, name) in enumerate(ROLES.items(), 1):
            config = settings.get("ports", {}).get(role, {})
            port = QComboBox()
            port.setEditable(True)
            port.setMinimumWidth(140)
            port.setCurrentText(config.get("port") or DEFAULT_PORTS[role])
            baud = QComboBox()
            for rate in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200):
                baud.addItem(str(rate), rate)
            baud.setCurrentIndex(max(0, baud.findData(config.get("baudrate", 9600))))
            unit = QSpinBox()
            unit.setRange(1, 247)
            unit.setValue(config.get("unit", 1))
            timeout = QDoubleSpinBox()
            timeout.setRange(0.02, 2)
            timeout.setDecimals(2)
            timeout.setSingleStep(0.05)
            timeout.setValue(config.get("timeout", 0.1))
            self.port_controls[role] = {
                "port": port,
                "baudrate": baud,
                "unit": unit,
                "timeout": timeout,
            }
            caption = QLabel(device_caption(role, port.currentText()))
            self.device_names[role] = caption
            port.currentTextChanged.connect(
                lambda value, r=role: self.device_names[r].setText(device_caption(r, value))
            )
            ports.addWidget(caption, row, 0)
            for col, widget in enumerate((port, baud, unit, timeout), 1):
                ports.addWidget(widget, row, col)
        meter_body.addWidget(port_card)
        refresh_row = QHBoxLayout()
        refresh_note = QLabel("刷新仅枚举系统串口，不连接设备。两组按各自控制器状态同步。")
        refresh_note.setObjectName("subtitle")
        refresh_note.setWordWrap(True)
        refresh_row.addWidget(refresh_note, 1)
        self.refresh_button = QPushButton("刷新串口")
        self.refresh_button.clicked.connect(self.refresh_ports)
        refresh_row.addWidget(self.refresh_button)
        body.addLayout(refresh_row)

        thresholds = card()
        grid = QGridLayout(thresholds)
        grid.setContentsMargins(15, 13, 15, 13)
        label = QLabel("可选报警阈值")
        label.setObjectName("section")
        grid.addWidget(label, 0, 0, 1, 4)
        self.threshold_enabled = {}
        self.threshold_inputs = {}
        for index, (key, text) in enumerate(
            (
                ("voltage_low", "电压下限 / V"),
                ("voltage_high", "电压上限 / V"),
                ("current_low", "电流下限 / A"),
                ("current_high", "电流上限 / A"),
            )
        ):
            enabled = QCheckBox(text)
            value = QDoubleSpinBox()
            value.setRange(-1000000, 1000000)
            value.setDecimals(3)
            enabled.setChecked(settings.get(key) is not None)
            value.setValue(settings.get(key) if settings.get(key) is not None else 0)
            value.setEnabled(enabled.isChecked())
            enabled.toggled.connect(value.setEnabled)
            self.threshold_enabled[key] = enabled
            self.threshold_inputs[key] = value
            row, col = index // 2 + 1, index % 2 * 2
            grid.addWidget(enabled, row, col)
            grid.addWidget(value, row, col + 1)
        note = QLabel("未勾选即不启用；超限仍保留原始读数。阈值为 0 时也可正常启用。")
        note.setObjectName("subtitle")
        grid.addWidget(note, 3, 0, 1, 4)
        meter_body.addWidget(thresholds)
        meter_body.addStretch()
        rules = QLabel(
            "每秒采样一次，电流全程记录。输出时长和电压有效时间可在“采样时间”调整。\n"
            "此处只保存软件参数；请在两台控制器上手动设置相同输出时长。"
        )
        rules.setObjectName("subtitle")
        rules.setWordWrap(True)
        body.addWidget(rules)
        self.error_label = QLabel()
        self.error_label.setStyleSheet("color:#b44747;")
        self.error_label.setWordWrap(True)
        body.addWidget(self.error_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用参数")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        body.addWidget(buttons)

        def sync_changed():
            automatic = self.sync_combo.currentData() == "controller"
            controller_tab.setEnabled(automatic)
            refresh_note.setText(
                "刷新仅枚举系统串口，不连接设备。两组按各自控制器状态同步。"
                if automatic
                else "刷新仅枚举系统串口，不连接设备。人工兼容模式开始后需手动同步。"
            )

        self.sync_combo.currentIndexChanged.connect(sync_changed)
        sync_changed()
        self.refresh_ports()

    def update_timing_note(self):
        width, start, end = (
            self.timing_inputs[key].value() for key in ("step_seconds", "valid_start", "valid_end")
        )
        self.timing_note.setText(
            f"当前：每路输出 {width:g} 秒，采用输出后第 {start:g} 秒起至第 {end:g} 秒前的电压。\n"
            "起始时刻包含，结束时刻不包含；一次读取须完整落在有效时间内。"
            "采样频率为每秒一次，窗口过短或通信异常时可能没有有效读数。\n"
            "默认：6 秒输出、取第 2～4 秒。运行期间不能修改，应用后用于下一次实验。\n"
            "请手动将两台控制器的输出时长设为相同值；不一致时该组电压不计入结果。"
        )

    def restore_timing(self):
        defaults = default_settings()
        for key, widget in self.timing_inputs.items():
            widget.setValue(defaults[key])

    def refresh_ports(self):
        # 保留用户输入，即使设备临时拔下，也不悄悄替换配置中的端口。
        try:
            detected = [(item.device, item.description) for item in list_ports.comports()]
        except (OSError, RuntimeError) as exc:
            self.error_label.setText(f"无法枚举串口：{exc}")
            return
        for controls in (*self.port_controls.values(), *self.controller_controls.values()):
            combo = controls["port"]
            current = combo.currentText()
            combo.clear()
            combo.addItem("")
            for name, description in detected:
                combo.addItem(name)
                combo.setItemData(combo.count() - 1, description, Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentText(current)

    def values(self):
        # 从原配置复制，保留经过契约确认的时序规则和绘图容量。
        """从控件生成独立配置字典，不打开串口或向控制器写参数。"""
        values = deepcopy(self._original)
        values["mode"] = "hardware"
        values.pop("simulation_speed", None)
        values["synchronization"] = self.sync_combo.currentData()
        values["controllers"] = controller_values(self.controller_controls)
        values.update({key: widget.value() for key, widget in self.timing_inputs.items()})
        values["ports"] = {}
        for role, controls in self.port_controls.items():
            values["ports"][role] = {
                "port": controls["port"].currentText().strip().upper(),
                "baudrate": controls["baudrate"].currentData(),
                "unit": controls["unit"].value(),
                "timeout": controls["timeout"].value(),
            }
        for key, checkbox in self.threshold_enabled.items():
            values[key] = self.threshold_inputs[key].value() if checkbox.isChecked() else None
        return values

    def accept(self):
        """先校验可执行配置，失败时保留对话框与输入，不悄悄采用错误参数。"""
        values = self.values()
        if values["mode"] == "hardware":
            ports = [config["port"] for config in values["ports"].values()]
            if not all(re.fullmatch(r"COM[1-9]\d*", port) for port in ports):
                self.error_label.setText("请为三台设备填写有效串口，如 COM3；串口不能为空。")
                return
            if len(set(ports)) != 3:
                self.error_label.setText("三台设备必须使用互不相同的串口。")
                return
        for prefix, title in (("voltage", "电压"), ("current", "电流")):
            low, high = values.get(f"{prefix}_low"), values.get(f"{prefix}_high")
            if low is not None and high is not None and low > high:
                self.error_label.setText(f"{title}下限不能大于上限。")
                return
        try:
            validate_settings(values, for_start=True)
        except (ValueError, TypeError) as exc:
            self.error_label.setText(str(exc))
            return
        self.error_label.clear()
        super().accept()


# 线程无父窗口，并在此强引用至结束，防止父窗口销毁时误析构仍在工作的 QThread。
_ACTIVE_EXPORTS = set()


class ClearHistoryWorker(QThread):
    """后台执行历史清空事务；只在用户确认且界面允许清空时启动。"""

    done = Signal(bool, str)

    def __init__(self, store):
        super().__init__()
        self.store = store

    def run(self):
        try:
            count = self.store.clear_history()
        except Exception as exc:
            self.done.emit(False, f"清空失败，记录已保留：{exc}")
        else:
            self.done.emit(True, f"已清空 {count} 次实验及其全部历史明细。")


class ExportWorker(QThread):
    """历史导出线程；用数据库读视图，不读取正在改变的表格控件。"""

    done = Signal(bool, str)

    def __init__(self, store, session_id, destination, filters):
        super().__init__()
        self.store = store
        self.session_id = session_id
        self.destination = destination
        self.filters = filters

    @Slot()
    def run(self):
        try:
            self.store.export_session(self.session_id, self.destination, **self.filters)
        except Exception as exc:
            self.done.emit(False, f"导出失败：{exc}")
        else:
            self.done.emit(True, f"导出完成：{self.destination}")


class HistoryDialog(QDialog):
    """提供按会话/时间/对象/有效性查询、分页导出与确认清空的历史入口。"""

    history_cleared = Signal()
    PAGE_SIZE = 100

    def __init__(self, store, parent=None, allow_clear=True):
        super().__init__(parent)
        self.store = store
        self.offset = 0
        self.session_id = None
        self.export_thread = None
        self.export_worker = None
        self.clear_thread = None
        self.allow_clear = allow_clear
        self.setWindowTitle("实验历史记录")
        self.resize(1120, 790)
        self.setMinimumSize(920, 670)
        self.setStyleSheet(
            STYLE + "\nQTableWidget {selection-color:#25394e;}"
            "QTableWidget::item:selected {background:#dcefeb;color:#25394e;}"
        )
        body = QVBoxLayout(self)
        body.setContentsMargins(23, 20, 23, 20)
        title_row = QHBoxLayout()
        title = QLabel("实验历史记录")
        title.setObjectName("section")
        title_row.addWidget(title)
        title_row.addStretch()
        refresh = QPushButton("刷新实验列表")
        refresh.clicked.connect(self.refresh_sessions)
        title_row.addWidget(refresh)
        self.clear_button = QPushButton("清空历史记录")
        self.clear_button.setObjectName("stop")
        self.clear_button.setEnabled(allow_clear)
        self.clear_button.setToolTip("停止采集并等待导出完成后，可清空全部实验历史")
        self.clear_button.clicked.connect(self.clear_history)
        title_row.addWidget(self.clear_button)
        body.addLayout(title_row)
        self.session_table = QTableWidget(0, 4)
        self.session_table.setHorizontalHeaderLabels(["开始时间", "结束时间", "状态", "实验编号"])
        self.session_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.session_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.session_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.session_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.session_table.verticalHeader().hide()
        self.session_table.setMaximumHeight(180)
        self.session_table.itemSelectionChanged.connect(self._select_session)
        body.addWidget(self.session_table)
        self.detail_label = QLabel("选择实验以查看明细。")
        self.detail_label.setWordWrap(True)
        self.detail_label.setObjectName("subtitle")
        body.addWidget(self.detail_label)

        filters = QGridLayout()
        self.start_input = QLineEdit()
        self.end_input = QLineEdit()
        for widget in (self.start_input, self.end_input):
            widget.setPlaceholderText("可留空 · 2026-10-16T00:00:00+08:00")
            widget.setMinimumWidth(275)
        filters.addWidget(QLabel("起始时间"), 0, 0)
        filters.addWidget(self.start_input, 0, 1)
        filters.addWidget(QLabel("结束时间"), 0, 2)
        filters.addWidget(self.end_input, 0, 3)
        self.cell_combo = QComboBox()
        self.cell_combo.addItem("全部小室与电流", None)
        self.cell_combo.addItem("电流", 0)
        for cell in range(1, 31):
            self.cell_combo.addItem(f"小室 {cell:02d}", cell)
        self.valid_only = QCheckBox("仅有效样本")
        self.valid_only.setChecked(True)
        filters.addWidget(QLabel("样本对象"), 1, 0)
        filters.addWidget(self.cell_combo, 1, 1)
        filters.addWidget(self.valid_only, 1, 2)
        query_button = QPushButton("应用筛选")
        query_button.setObjectName("primary")
        query_button.clicked.connect(self.query)
        filters.addWidget(query_button, 1, 3)
        body.addLayout(filters)
        self.sample_table = QTableWidget(0, 7)
        self.sample_table.setHorizontalHeaderLabels(
            ["采样时间", "实验秒", "对象", "读数", "累计均值", "有效性", "状态"]
        )
        self.sample_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.sample_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.sample_table.verticalHeader().hide()
        self.sample_table.setAlternatingRowColors(True)
        self.sample_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        body.addWidget(self.sample_table, 1)
        pages = QHBoxLayout()
        self.prev_button = QPushButton("上一页")
        self.next_button = QPushButton("下一页")
        self.prev_button.clicked.connect(self.previous_page)
        self.next_button.clicked.connect(self.next_page)
        pages.addWidget(self.prev_button)
        self.page_label = QLabel("第 1 页 · 每页 100 条")
        pages.addWidget(self.page_label)
        pages.addWidget(self.next_button)
        pages.addStretch()
        self.export_button = QPushButton("导出筛选结果…")
        self.export_button.clicked.connect(self.choose_export)
        pages.addWidget(self.export_button)
        body.addLayout(pages)
        self.status_label = QLabel("导出包含实验信息与计算口径；完整历史保存在数据库。")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("subtitle")
        body.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        body.addWidget(buttons)
        self.refresh_sessions()

    def refresh_sessions(self):
        try:
            sessions = [s for s in self.store.list_sessions() if s.get("mode") == "hardware"]
        except Exception as exc:
            self.status_label.setText(f"读取实验列表失败：{exc}")
            return
        self.session_table.blockSignals(True)
        self.session_table.setRowCount(len(sessions))
        for row, session in enumerate(sessions):
            values = [
                session.get("started_at"),
                session.get("ended_at") or "尚未结束",
                {
                    "running": "运行中",
                    "completed": "已完成",
                    "failed": "异常",
                    "error": "异常",
                    "interrupted": "中断",
                }.get(session.get("status"), session.get("status", "—")),
                session["id"],
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or "—"))
                item.setData(Qt.ItemDataRole.UserRole, session["id"])
                self.session_table.setItem(row, col, item)
        self.session_table.blockSignals(False)
        if sessions:
            self.session_table.selectRow(0)
        else:
            self.session_id = None
            self.sample_table.setRowCount(0)
            self.detail_label.setText("暂无实验记录。")
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
        self.export_button.setEnabled(
            bool(self.session_id) and self.export_thread is None and self.clear_thread is None
        )

    def clear_history(self):
        """默认取消的二次确认后启动清空；采集或导出期间不能进入删除事务。"""
        if (
            not self.allow_clear
            or self.clear_thread is not None
            or self.export_thread is not None
            or _ACTIVE_EXPORTS
        ):
            self.status_label.setText("请先停止采集并等待导出完成，再清空历史记录。")
            return
        try:
            sessions = self.store.list_sessions()
        except Exception as exc:
            self.status_label.setText(f"读取历史记录失败：{exc}")
            return
        if not sessions:
            self.status_label.setText("暂无历史记录，无需清空。")
            return
        answer = QMessageBox.question(
            self,
            "确认清空历史记录",
            "将永久清空全部实验、测量样本、控制器状态及实验明细，无法撤销。\n"
            "已导出的 TXT / Excel 文件不会删除。需要保留的数据请先导出。\n\n确定清空全部历史记录吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        thread = ClearHistoryWorker(self.store)
        self.clear_thread = thread
        thread.done.connect(self._clear_result)
        thread.finished.connect(self._clear_finished)
        thread.finished.connect(thread.deleteLater)
        self.clear_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.status_label.setText("正在清空历史记录，请等待完成…")
        thread.start()

    @Slot(bool, str)
    def _clear_result(self, success, message):
        if success:
            self.offset = 0
            self.session_id = None
            self.refresh_sessions()
            self.page_label.setText("第 1 页 · 每页 100 条")
            self.history_cleared.emit()
        self.status_label.setText(message)

    @Slot()
    def _clear_finished(self):
        self.clear_thread = None
        self.clear_button.setEnabled(self.allow_clear)
        self.export_button.setEnabled(bool(self.session_id))

    def _select_session(self):
        row = self.session_table.currentRow()
        if row < 0:
            return
        self.session_id = self.session_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        try:
            session = self.store.get_session(self.session_id)
            snapshot = session.get("snapshot") or {}
            metrics = snapshot.get("metrics") or {}
            self.detail_label.setText(
                f"实验 {self.session_id}   ·   最终总压 {number(metrics.get('total_voltage'))} V   ·   累计平均功率 {number(metrics.get('avg_power_kw'))} kW"
            )
        except Exception as exc:
            self.detail_label.setText(f"实验详情读取失败：{exc}")
        self.query()

    def _filters(self):
        """将界面筛选转换为存储接口参数，时间含时区，小室0表示整组电流。"""
        values = {
            "start": self.start_input.text().strip() or None,
            "end": self.end_input.text().strip() or None,
            "cell": self.cell_combo.currentData(),
            "valid_only": self.valid_only.isChecked(),
        }
        parsed = {}
        for key in ("start", "end"):
            if values[key]:
                try:
                    parsed[key] = datetime.fromisoformat(values[key])
                    if parsed[key].tzinfo is None:
                        raise ValueError("缺少时区")
                except ValueError as exc:
                    raise ValueError(
                        "时间请填写带时区的 ISO 格式，例如 2026-10-16T00:00:00+08:00。"
                    ) from exc
        if len(parsed) == 2 and parsed["start"] > parsed["end"]:
            raise ValueError("起始时间不能晚于结束时间。")
        return values

    def query(self):
        self.offset = 0
        self._load_page()

    def _load_page(self):
        """只加载当前页；完整实验数据留在SQLite，避免长时记录塞满表格内存。"""
        if not self.session_id:
            self.status_label.setText("请先选择实验。")
            return
        try:
            rows = self.store.query_samples(
                self.session_id, **self._filters(), limit=self.PAGE_SIZE + 1, offset=self.offset
            )
        except Exception as exc:
            self.status_label.setText(f"查询失败：{exc}")
            return
        self.prev_button.setEnabled(self.offset > 0)
        self.next_button.setEnabled(len(rows) > self.PAGE_SIZE)
        rows = rows[: self.PAGE_SIZE]
        self.sample_table.setRowCount(len(rows))
        for row, sample in enumerate(rows):
            current = sample.get("role") == "current"
            unit = "A" if current else "V"
            values = [
                sample.get("wall_time", "—"),
                number(sample.get("elapsed"), 1),
                "电流" if current else f"小室 {sample.get('cell') or '—'}",
                f"{number(sample.get('value'))} {unit}",
                f"{number(sample.get('mean_value'))} {unit}",
                "有效" if sample.get("valid") else "无效",
                {
                    "valid": "有效",
                    "error": "仪表读取失败",
                    "invalid_time": "采样时间无效",
                    "invalid_value": "仪表数值无效",
                    "unsynchronized": "尚未同步",
                    "out_of_window": "非有效采样窗口",
                    "outside_window": "非有效采样窗口",
                    "controller_unconfirmed": "等待控制器确认",
                    "unconfirmed": "等待控制器确认",
                    "invalid_controller_evidence": "同步证据无效",
                    "no_bracket": "缺少前后同步状态",
                    "stale": "同步状态已过期",
                    "transition": "读取跨通道切换",
                    "frozen": "倒计时停滞",
                    "inconsistent": "控制器时序不一致",
                    "read_error": "控制器读取失败",
                    "waiting": "控制器间隔或停止",
                    "invalid_parameters": "控制器参数不符",
                    "invalid_registers": "控制器状态无效",
                    "slow_read": "控制器读取过慢",
                }.get(sample.get("status"), sample.get("status", "—")),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(sample.get("error") or ""))
                self.sample_table.setItem(row, col, item)
        self.page_label.setText(
            f"第 {self.offset // self.PAGE_SIZE + 1} 页 · 每页 {self.PAGE_SIZE} 条"
        )
        self.status_label.setText(
            f"本页 {len(rows)} 条样本。读数与均值保留原始有效性；导出使用当前筛选条件。"
        )

    def next_page(self):
        self.offset += self.PAGE_SIZE
        self._load_page()

    def previous_page(self):
        self.offset = max(0, self.offset - self.PAGE_SIZE)
        self._load_page()

    def choose_export(self):
        if not self.session_id:
            self.status_label.setText("请先选择实验。")
            return
        destination, selected = QFileDialog.getSaveFileName(
            self, "导出实验历史", "实验历史.xlsx", "Excel 工作簿 (*.xlsx);;UTF-8 文本 (*.txt)"
        )
        if destination:
            if not destination.lower().endswith((".xlsx", ".txt")):
                destination += ".txt" if "*.txt" in selected else ".xlsx"
            self.start_export(destination)

    def start_export(self, destination):
        """冻结当前筛选参数并启动后台导出，完成前禁止销毁该线程所属窗口。"""
        if self.export_thread is not None or self.clear_thread is not None or not self.session_id:
            return
        try:
            filters = self._filters()
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        # 每个导出线程调用存储层的独立连接，主线程不遍历全部历史样本。
        thread = ExportWorker(self.store, self.session_id, destination, filters)
        self.export_thread, self.export_worker = thread, thread
        _ACTIVE_EXPORTS.add(thread)
        thread.done.connect(self._export_result)
        thread.finished.connect(self._export_finished)
        thread.finished.connect(lambda: _ACTIVE_EXPORTS.discard(thread))
        thread.finished.connect(thread.deleteLater)
        self.export_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self.status_label.setText("正在后台导出，请等待完成后关闭此窗口…")
        thread.start()

    @Slot(bool, str)
    def _export_result(self, success, message):
        self.status_label.setText(message)

    @Slot()
    def _export_finished(self):
        self.export_thread = None
        self.export_worker = None
        self.export_button.setEnabled(True)
        self.clear_button.setEnabled(self.allow_clear)

    def reject(self):
        if self.clear_thread is not None:
            self.status_label.setText("正在清空历史记录，请等待完成后关闭。")
            return
        if self.export_thread is not None:
            self.status_label.setText("正在导出，请等待完成后关闭；导出数据不会被中断。")
            return
        super().reject()

    def closeEvent(self, event):
        if self.clear_thread is not None:
            self.status_label.setText("正在清空历史记录，请等待完成后关闭。")
            event.ignore()
            return
        if self.export_thread is not None:
            self.status_label.setText("正在导出，请等待完成后关闭；导出数据不会被中断。")
            event.ignore()
        else:
            event.accept()
