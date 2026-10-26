"""主窗口只消费采集快照；统计与完整历史由引擎、数据库负责。"""

from collections import deque
import math

import pyqtgraph as pg
from .alerts import AlarmNotifier
from .config import ROLES as ROLE_ORDER, device_caption
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


ROLES = {role: device_caption(role) for role in ROLE_ORDER}
DISPLAY_ROLES = (*ROLE_ORDER, "controller_left", "controller_right")
STATE_NAMES = {
    "connected": "已连接",
    "connecting": "连接中",
    "disconnected": "连接断开",
    "stopped": "待机",
    "stopping": "正在停止读取…",
    "saving": "正在保存数据…",
    "disabled": "未启用",
}
METRICS = [
    ("current", "实时电流", "A"),
    ("total_voltage", "总电压", "V"),
    ("avg_total_voltage", "累计平均总压", "V"),
    ("power_kw", "实时功率", "kW"),
    ("avg_power_kw", "累计平均功率", "kW"),
]

STYLE = """
QWidget {font-family:'Microsoft YaHei UI','Microsoft YaHei';font-size:13px;color:#25394e;}
QMainWindow,QDialog {background:#eff3f8;}
QLabel#title {font-size:25px;font-weight:700;color:#102c46;}
QLabel#subtitle {color:#75859a;font-size:12px;}
QLabel#section {font-size:15px;font-weight:700;color:#183b54;}
QLabel#metricValue {font-size:30px;font-weight:700;color:#0d756e;}
QLabel#metricName {font-size:12px;color:#657990;}
QLabel#metricUnit {font-size:13px;color:#8290a1;}
QFrame#card {background:white;border:1px solid #dce4ed;border-radius:9px;}
QLabel#badge {border-radius:6px;padding:7px 13px;font-size:13px;font-weight:700;}
QPushButton {background:white;border:1px solid #cfdae6;border-radius:6px;padding:8px 17px;font-weight:600;}
QPushButton:hover {background:#eaf4f5;border-color:#72ada9;}
QPushButton:pressed {background:#d3ece9;}
QPushButton:disabled {color:#aab5c2;background:#edf1f5;border-color:#dce3ec;}
QPushButton#primary {background:#087f78;color:white;border:1px solid #087f78;}
QPushButton#primary:hover {background:#066b65;}
QPushButton#primary:disabled {background:#acd5d0;border-color:#acd5d0;}
QPushButton#stop {color:#b94848;border-color:#e7bcbc;}
QPushButton#stop:disabled {color:#b8a7a7;border-color:#dfd9dd;}
QTabWidget::pane {border:0px;}
QTabBar::tab {background:transparent;padding:11px 25px;border-bottom:3px solid transparent;color:#718196;font-weight:600;}
QTabBar::tab:selected {color:#087f78;border-bottom:3px solid #087f78;}
QTabBar::tab:hover {background:#e6eef5;}
QTableWidget {background:white;alternate-background-color:#f7f9fc;border:0px;gridline-color:#edf1f5;selection-background-color:#dcefeb;}
QTableWidget::item {padding:3px 10px;border-bottom:1px solid #eff3f7;}
QHeaderView::section {background:#f0f5f9;color:#63778d;border:0px;border-bottom:1px solid #dfe7ef;padding:9px 6px;font-weight:600;}
QTextEdit {background:#f8fafc;border:1px solid #e1e8f0;border-radius:5px;padding:4px;color:#65768b;font-size:11px;}
QComboBox,QSpinBox,QDoubleSpinBox,QLineEdit {background:white;border:1px solid #cfdbe7;border-radius:5px;padding:6px;min-height:22px;}
QComboBox:disabled,QSpinBox:disabled,QDoubleSpinBox:disabled {background:#edf1f5;color:#9aa7b5;}
QComboBox QAbstractItemView {background-color:#ffffff;color:#25394e;alternate-background-color:#f7f9fc;selection-background-color:#dcefeb;selection-color:#0b605a;border:1px solid #cfdbe7;outline:0px;padding:3px;}
QComboBox QAbstractItemView::item {background-color:#ffffff;color:#25394e;min-height:26px;padding:4px 8px;}
QComboBox QAbstractItemView::item:selected,QComboBox QAbstractItemView::item:hover {background-color:#dcefeb;color:#0b605a;}
QScrollArea {background:transparent;border:0px;}
QScrollBar:vertical {background:#edf2f7;width:9px;margin:0px;}
QScrollBar::handle:vertical {background:#c2cfdd;border-radius:4px;min-height:30px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0px;}
QCheckBox {spacing:6px;}
"""


def number(value, precision=3):
    """缺测值与非有限数统一显示空位，真实的零仍显示为零。"""
    if isinstance(value, (int, float)) and math.isfinite(value):
        return f"{value:.{precision}f}"
    return "—"


def card():
    frame = QFrame()
    frame.setObjectName("card")
    return frame


class TrendChart(QFrame):
    """仅缓存绘图所需尾部数据；均值直接采用引擎输出，不在界面重算。"""

    def __init__(self, title, unit, max_points, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.title = title
        self.detail_dialog = None
        self.times = deque(maxlen=max_points)
        self.latest_values = deque(maxlen=max_points)
        self.mean_values = deque(maxlen=max_points)
        self.setMinimumHeight(172)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 5)
        header = QHBoxLayout()
        label = QLabel(title)
        label.setStyleSheet("font-weight:700;color:#203e56;font-size:12px;")
        header.addWidget(label)
        header.addStretch()
        self.latest_label = QLabel(f"最新 — {unit}")
        self.latest_label.setStyleSheet("color:#087f78;font-size:10px;")
        self.mean_label = QLabel(f"均值 — {unit}")
        self.mean_label.setStyleSheet("color:#d49935;font-size:10px;")
        self.unit = unit
        header.addWidget(self.latest_label)
        header.addWidget(self.mean_label)
        enlarge = QPushButton("放大")
        enlarge.setToolTip("也可直接单击曲线放大；放大后实时更新")
        enlarge.setStyleSheet("padding:3px 7px;font-size:11px;")
        enlarge.clicked.connect(self.open_detail)
        header.addWidget(enlarge)
        layout.addLayout(header)
        self.plot = pg.PlotWidget(background="white")
        self.plot.setMinimumHeight(115)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.scene().sigMouseClicked.connect(self._plot_clicked)
        self.plot.setLabel("bottom", "实验时间", units="s", color="#8a99a9", **{"font-size": "9px"})
        for name in ("left", "bottom"):
            axis = self.plot.getAxis(name)
            axis.setPen("#c9d5df")
            axis.setTextPen("#8496a8")
            axis.setStyle(tickFont=QFont("Microsoft YaHei UI", 8))
        self.latest_curve = self.plot.plot(pen=pg.mkPen("#098c82", width=1.8), connect="finite")
        self.mean_curve = self.plot.plot(
            pen=pg.mkPen("#d49935", width=1.3, style=Qt.PenStyle.DashLine), connect="finite"
        )
        layout.addWidget(self.plot)

    def _plot_clicked(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_detail()

    def open_detail(self):
        if self.detail_dialog is None:
            from .chart_detail import ChartDetail

            self.detail_dialog = ChartDetail(self, self.window())
            self.detail_dialog.finished.connect(self._detail_closed)
        self.detail_dialog.show()
        self.detail_dialog.raise_()
        self.detail_dialog.activateWindow()

    def _detail_closed(self, *_):
        self.detail_dialog = None

    def append(self, elapsed, value, mean=None):
        if number(elapsed) == "—":
            return
        if number(value) == "—":
            if not self.times:
                return
            # 缺测必须保留断点，恢复后不能用连线暗示故障期间仍有有效读数。
            value = float("nan")
        self.times.append(elapsed)
        self.latest_values.append(value)
        self.mean_values.append(mean if number(mean) != "—" else float("nan"))
        self.latest_curve.setData(list(self.times), list(self.latest_values))
        self.mean_curve.setData(list(self.times), list(self.mean_values))
        self.latest_label.setText(f"最新 {number(value)} {self.unit}")
        self.mean_label.setText(f"均值 {number(mean)} {self.unit}")
        if self.detail_dialog is not None:
            self.detail_dialog.refresh()

    def clear(self):
        self.times.clear()
        self.latest_values.clear()
        self.mean_values.clear()
        self.latest_curve.clear()
        self.mean_curve.clear()
        self.latest_label.setText(f"最新 — {self.unit}")
        self.mean_label.setText(f"均值 — {self.unit}")
        if self.detail_dialog is not None:
            self.detail_dialog.refresh()


class MainWindow(QMainWindow):
    """只消费快照与样本的显示层；不承担硬件通信、有效性判定或累计计算。"""

    start_requested = Signal()
    stop_requested = Signal()
    sync_requested = Signal()
    settings_requested = Signal()
    export_requested = Signal()
    history_requested = Signal()
    close_requested = Signal()

    def __init__(self, settings):
        super().__init__()
        self.settings = dict(settings)
        self.setWindowTitle("重庆大学电解水制氢系统")
        self.setMinimumSize(1120, 680)
        available = self.screen().availableGeometry()
        self.resize(min(1440, available.width()), min(1000, available.height() - 40))
        self.setStyleSheet(STYLE)
        self.running = False
        self._current_from_samples = False
        self._closing = False
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 17, 24, 12)
        layout.setSpacing(12)
        title = QLabel("重庆大学电解水制氢系统")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        self.run_label = QLabel("●  待机")
        self.run_label.setStyleSheet("color:#65788e;font-weight:600;padding-left:15px;")

        toolbar = QHBoxLayout()
        actions = [
            ("start_button", "开始实验", "start_requested", "primary"),
            ("stop_button", "停止采集", "stop_requested", "stop"),
            ("sync_button", "手动同步", "sync_requested", ""),
            ("settings_button", "参数设置", "settings_requested", ""),
        ]
        for attr, title_text, signal, object_name in actions:
            button = QPushButton(title_text)
            button.setObjectName(object_name)
            button.clicked.connect(getattr(self, signal).emit)
            setattr(self, attr, button)
            toolbar.addWidget(button)
        self.sync_button.setToolTip("确认继电器当前接通 1 与 16 时点击；将重新形成完整扫描。")
        toolbar.addStretch()
        toolbar.addWidget(self.run_label)
        self.alarm_button = QPushButton("报警 0")
        self.alarm_button.setToolTip("查看当前报警；确认或关闭弹窗只会停止声音")
        toolbar.addWidget(self.alarm_button)
        self.alarm_notifier = AlarmNotifier(self, self.alarm_button)
        for attr, text, signal in (
            ("export_button", "保存数据", "export_requested"),
            ("history_button", "历史记录", "history_requested"),
        ):
            button = QPushButton(text)
            button.clicked.connect(getattr(self, signal).emit)
            setattr(self, attr, button)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)

        devices = QHBoxLayout()
        self.device_labels = {}
        self.device_names = {}
        self._device_statuses = {}
        self._stop_phase = None
        for role in DISPLAY_ROLES:
            frame = card()
            # 五个接口采用相同的两行卡片，小屏下名称和状态也不会互相挤压。
            row = QVBoxLayout(frame)
            row.setContentsMargins(12, 8, 12, 8)
            row.setSpacing(4)
            caption = QLabel(device_caption(role, self._device_port(role)))
            self.device_names[role] = caption
            row.addWidget(caption)
            state = QLabel("●  待机")
            self.device_labels[role] = state
            row.addWidget(state)
            devices.addWidget(frame, 1)
        layout.addLayout(devices)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._numerical_page(), "实时数据")
        self.tabs.addTab(self._charts_page(), "趋势曲线")
        layout.addWidget(self.tabs, 1)
        self.event_log = QTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.document().setMaximumBlockCount(300)
        self.event_log.setFixedHeight(63)
        self.event_log.setPlaceholderText("运行事件将在此显示；完整采集记录保存在实验数据库中。")
        layout.addWidget(self.event_log)
        footer = QHBoxLayout()
        label = QLabel()
        self.footer_label = label
        label.setObjectName("subtitle")
        footer.addWidget(label)
        footer.addStretch()
        self.elapsed_label = QLabel("实验时长  00:00:00")
        self.elapsed_label.setObjectName("subtitle")
        footer.addWidget(self.elapsed_label)
        layout.addLayout(footer)
        self.reset_session("hardware")
        self.set_running(False)

    def _numerical_page(self):
        page = QWidget()
        vertical = QVBoxLayout(page)
        vertical.setContentsMargins(0, 13, 0, 0)
        vertical.setSpacing(12)
        scan = QHBoxLayout()
        section = QLabel("小室电压监测")
        section.setObjectName("section")
        scan.addWidget(section)
        self.current_measurement_label = QLabel("当前测量：待机")
        self.current_measurement_label.setContentsMargins(20, 0, 0, 0)
        scan.addWidget(self.current_measurement_label)
        scan.addStretch()
        self.scan_label = QLabel("本轮进度 0/30 · 待同步")
        self.scan_label.setStyleSheet("color:#618091;font-size:12px;")
        scan.addWidget(self.scan_label)
        vertical.addLayout(scan)
        self.controller_labels = {}
        self.cell_indicators = {}
        self._controller_positions = {"left": 0, "right": 0}
        tables = QHBoxLayout()
        tables.setSpacing(16)
        for start, attr, caption in (
            (1, "left_table", "A 组  /  01—15"),
            (16, "right_table", "B 组  /  16—30"),
        ):
            frame = card()
            box = QVBoxLayout(frame)
            box.setContentsMargins(0, 0, 0, 0)
            group = QLabel(caption)
            group.setStyleSheet(
                "font-weight:700;color:#47627b;padding:10px 15px;background:#f9fbfd;"
            )
            group.setWordWrap(True)
            self.controller_labels["left" if start == 1 else "right"] = group
            box.addWidget(group)
            table = QTableWidget(15, 3)
            table.setHorizontalHeaderLabels(["编号", "最新电压 / V", "累计平均 / V"])
            table.verticalHeader().hide()
            table.verticalHeader().setDefaultSectionSize(24)
            table.verticalHeader().setMinimumSectionSize(22)
            table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.setShowGrid(False)
            table.setAlternatingRowColors(True)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            for row in range(15):
                for col in range(3):
                    item = QTableWidgetItem(f"{start + row:02d}" if col == 0 else "—")
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if col == 0:
                        item.setForeground(QColor("#6c8095"))
                    elif col == 1:
                        item.setFont(QFont("Microsoft YaHei UI", 11, QFont.Weight.DemiBold))
                        item.setForeground(QColor("#143e54"))
                    else:
                        item.setForeground(QColor("#63847f"))
                    table.setItem(row, col, item)
                # 指示灯和编号放在同一格，保留电压、均值列及表格原来的宽度。
                cell = start + row
                holder = QWidget()
                holder.setStyleSheet("background:transparent;")
                cell_row = QHBoxLayout(holder)
                cell_row.setContentsMargins(0, 0, 0, 0)
                cell_row.setSpacing(9)
                cell_row.addStretch()
                lamp = QLabel()
                lamp.setObjectName(f"cellIndicator{cell}")
                lamp.setFixedSize(12, 12)
                self.cell_indicators[cell] = lamp
                cell_row.addWidget(lamp)
                caption = QLabel(f"{cell:02d}")
                caption.setStyleSheet("color:#6c8095;background:transparent;")
                cell_row.addWidget(caption)
                cell_row.addStretch()
                table.setCellWidget(row, 0, holder)
                # 编号由上面的标签绘制，底层保留文本供表格访问，避免透明控件叠字。
                table.item(row, 0).setForeground(QColor(Qt.GlobalColor.transparent))
            # 小屏幕保留表内滚动，避免底部指标和停止按钮被挤出屏幕。
            table.setMinimumHeight(240)
            box.addWidget(table)
            setattr(self, attr, table)
            tables.addWidget(frame)
        vertical.addLayout(tables, 1)
        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.metric_values = {}
        for key, name, unit in METRICS:
            frame = card()
            stack = QVBoxLayout(frame)
            stack.setContentsMargins(16, 12, 16, 12)
            title_row = QHBoxLayout()
            title = QLabel(name)
            title.setObjectName("metricName")
            title_row.addWidget(title)
            title_row.addStretch()
            unit_label = QLabel(unit)
            unit_label.setObjectName("metricUnit")
            title_row.addWidget(unit_label)
            stack.addLayout(title_row)
            value = QLabel("—")
            value.setObjectName("metricValue")
            self.metric_values[key] = value
            stack.addWidget(value)
            metrics.addWidget(frame)
        vertical.addLayout(metrics)
        return page

    def _charts_page(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 8, 0, 0)
        controls = QHBoxLayout()
        self.chart_measurement_label = QLabel("当前测量：待机")
        controls.addWidget(self.chart_measurement_label)
        self.chart_search = QLineEdit()
        self.chart_search.setPlaceholderText("搜索小室编号或名称，例如 16、1-5、电流、功率")
        self.chart_search.setClearButtonEnabled(True)
        controls.addWidget(self.chart_search, 1)
        self.chart_group = QComboBox()
        for text, key in [
            ("全部曲线", "all"),
            ("左组 01—15", "left"),
            ("右组 16—30", "right"),
            ("汇总曲线", "summary"),
        ]:
            self.chart_group.addItem(text, key)
        controls.addWidget(self.chart_group)
        self.chart_count = QLabel()
        controls.addWidget(self.chart_count)
        page_layout.addLayout(controls)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setStyleSheet("background:transparent;")
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 12, 5, 5)
        body.setSpacing(13)
        capacity = max(1, int(self.settings.get("plot_points", 2000)))
        self.summary_charts = {}
        title = QLabel("实验汇总趋势")
        title.setObjectName("section")
        self.summary_heading = title
        body.addWidget(title)
        summary = QHBoxLayout()
        for key, name, unit in (
            ("total_voltage", "总电压", "V"),
            ("current", "电流", "A"),
            ("power_kw", "功率", "kW"),
        ):
            chart = TrendChart(name, unit, capacity)
            self.summary_charts[key] = chart
            summary.addWidget(chart)
        body.addLayout(summary)
        note = QLabel(
            "绿色：最新有效样本    金色虚线：本实验累计均值    ·    横轴为实验秒数；缺测不补点"
        )
        note.setObjectName("subtitle")
        body.addWidget(note)
        columns = QHBoxLayout()
        self.cell_charts = {}
        self.chart_group_panels = {}
        for start, title_text in ((1, "A 组  /  小室 01—15"), (16, "B 组  /  小室 16—30")):
            panel = QWidget()
            group = QVBoxLayout(panel)
            group.setContentsMargins(0, 0, 0, 0)
            self.chart_group_panels["left" if start == 1 else "right"] = panel
            label = QLabel(title_text)
            label.setObjectName("section")
            group.addWidget(label)
            grid = QGridLayout()
            grid.setSpacing(10)
            for index in range(15):
                cell = start + index
                chart = TrendChart(f"{cell:02d}  小室", "V", capacity)
                chart.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                self.cell_charts[cell] = chart
                grid.addWidget(chart, index // 2, index % 2)
            group.addLayout(grid)
            columns.addWidget(panel)
        body.addLayout(columns)
        scroll.setWidget(content)
        page_layout.addWidget(scroll, 1)
        self.chart_search.textChanged.connect(self.filter_charts)
        self.chart_group.currentIndexChanged.connect(self.filter_charts)
        self.filter_charts()
        return page

    def filter_charts(self, *_):
        """按组别和名称/编号范围筛选既有33张图，不改变数据库或后台采样。"""
        import re

        query = self.chart_search.text().strip().lower()
        group = self.chart_group.currentData()
        numbers = None
        if re.fullmatch(r"[0-9\s,，、\-—~～]+", query):
            numbers = set()
            for token in re.split(r"[,，、\s]+", query):
                if not token:
                    continue
                limits = re.split(r"[-—~～]", token)
                if len(limits) == 2 and all(limits):
                    begin, end = map(int, limits)
                    numbers.update(range(max(1, begin), min(30, end) + 1))
                elif len(limits) == 1:
                    numbers.add(int(token))
        visible = 0
        for cell, chart in self.cell_charts.items():
            matches = (
                not query or cell in numbers
                if numbers is not None
                else not query or query in chart.title.lower()
            )
            show = group in ("all", "left" if cell <= 15 else "right") and matches
            chart.setVisible(show)
            visible += int(show)
        for key, panel in self.chart_group_panels.items():
            panel.setVisible(
                any(
                    not chart.isHidden()
                    for cell, chart in self.cell_charts.items()
                    if (cell <= 15) == (key == "left")
                )
            )
        summary_visible = 0
        for chart in self.summary_charts.values():
            show = group in ("all", "summary") and (not query or query in chart.title.lower())
            chart.setVisible(show)
            summary_visible += int(show)
        self.summary_heading.setVisible(summary_visible > 0)
        self.chart_count.setText(f"显示 {visible + summary_visible}/33 张")

    def _controller_mode(self):
        return self.settings.get("synchronization", "manual") == "controller"

    def set_running(self, running, waiting_sync=False):
        """统一采集/待机按钮与提示，并清除上一次停止阶段的显示覆盖。"""
        self.running = running
        if not running:
            self.alarm_notifier.stop()
        self._stop_phase = None
        self.set_devices(self._device_statuses)
        automatic = self._controller_mode()
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.sync_button.setEnabled(running and not automatic)
        self.sync_button.setText("控制器同步" if automatic else "手动同步")
        self.sync_button.setToolTip(
            "两组独立读取控制器状态；连接异常时暂停该组有效电压采纳。"
            if automatic
            else "确认继电器当前接通 1 与 16 时点击；将重新形成完整扫描。"
        )
        self.settings_button.setEnabled(not running)
        self.export_button.setEnabled(True)
        self.run_label.setText(
            "●  等待控制器"
            if automatic and running
            else "●  等待人工同步"
            if waiting_sync
            else "●  采集中"
            if running
            else "●  待机"
        )
        self.run_label.setStyleSheet(
            f"color:{'#ac751d' if waiting_sync else '#087f78' if running else '#65788e'};font-weight:600;padding-left:15px;"
        )
        self.footer_label.setText(
            f"输出{self.settings.get('step_seconds', 6):g}秒 · "
            f"有效窗口{self.settings.get('valid_start', 2):g}≤t<{self.settings.get('valid_end', 4):g}s · 1Hz"
            if automatic
            else f"电压有效窗口 {self.settings.get('valid_start', 2):g} ≤ t < {self.settings.get('valid_end', 4):g} s"
            f"   ·   每组 {self.settings.get('step_seconds', 6):g} s   ·   采样 1 Hz"
        )
        for group, label in self.controller_labels.items():
            label.setVisible(True)
            if not automatic:
                label.setText("A 组  /  01—15" if group == "left" else "B 组  /  16—30")
        self._set_measurement_indicators()

    def set_stopping(self, phase="stopping"):
        """停止阶段优先覆盖普通连接包；指示灯熄灭，开始和导出保持禁用。"""
        self.running = False
        self.alarm_notifier.stop()
        self._stop_phase = phase
        for button in (
            self.start_button,
            self.stop_button,
            self.sync_button,
            self.settings_button,
            self.export_button,
        ):
            button.setEnabled(False)
        self.run_label.setText(f"●  {STATE_NAMES[phase]}")
        self.set_devices(self._device_statuses)
        self.run_label.setStyleSheet("color:#ac751d;font-weight:600;padding-left:15px;")
        self._set_measurement_indicators()

    def set_saving(self):
        """只有读取和尾包处理已结束才由协调器调用，显示真实的最后写盘阶段。"""
        self.set_stopping("saving")

    def _set_measurement_indicators(self, controllers=None, pair=(), synchronized=False):
        """仅用本次状态点亮对应行；停止、失联或组号不符时不延用上一盏灯。"""
        active = set()
        estimated = not self._controller_mode() and controllers is None
        if not self.running:
            text = "当前测量：待机"
        elif not estimated:
            captions = []
            for group, caption, low, high in (("left", "A", 1, 15), ("right", "B", 16, 30)):
                state = (controllers or {}).get(group) or {}
                cell = state.get("cell")
                stage = state.get("stage") or state.get("phase")
                status = state.get("status")
                reliable = status in ("valid", "ok")
                if (
                    reliable
                    and state.get("active")
                    and isinstance(cell, int)
                    and not isinstance(cell, bool)
                    and low <= cell <= high
                    and stage
                    not in ("interval", "cycle_interval", "cycle_wait", "stopped", "unknown")
                ):
                    active.add(cell)
                    position = f"第{cell:02d}小室"
                elif stage == "interval" and status in ("interval", "waiting", "valid", "ok"):
                    position = "切换间隔"
                elif stage in ("cycle_interval", "cycle_wait") and status in (
                    "cycle_interval",
                    "cycle_wait",
                    "waiting",
                    "valid",
                    "ok",
                ):
                    position = "周期等待"
                elif stage == "stopped" and status in ("stopped", "waiting", "valid", "ok"):
                    position = "未接通"
                else:
                    position = "等待状态" if not state else "状态待确认"
                captions.append(f"{caption}组 {position}")
            text = "当前测量：" + "  ｜  ".join(captions)
        elif synchronized:
            active = {
                cell
                for cell in pair
                if isinstance(cell, int) and not isinstance(cell, bool) and 1 <= cell <= 30
            }
            text = (
                "当前测量（按时间推算）："
                + "  ｜  ".join(f"第{cell:02d}小室" for cell in sorted(active))
                if active
                else "当前测量：等待人工同步"
            )
        else:
            text = "当前测量：等待人工同步"
        for label in (self.current_measurement_label, self.chart_measurement_label):
            label.setText(text)
            label.setStyleSheet(f"font-weight:700;color:{'#087f78' if active else '#65788e'};")
            label.setToolTip(
                "绿灯：当前正在测量；灰灯：未测量。电压采用输出后 "
                f"{self.settings.get('valid_start', 2):g}≤t<{self.settings.get('valid_end', 4):g} 秒的有效窗口。"
            )
        for cell, lamp in self.cell_indicators.items():
            on = cell in active
            lamp.setProperty("active", on)
            lamp.setStyleSheet(
                "background:#12b896;border:2px solid #087f78;border-radius:6px;"
                if on
                else "background:#e2e8ee;border:1px solid #b9c7d3;border-radius:6px;"
            )
            source = "按时间推算" if estimated else "控制器状态"
            lamp.setToolTip(
                f"第{cell:02d}小室：正在测量（{source}）" if on else f"第{cell:02d}小室：未测量"
            )

    def _render_controllers(self, controllers):
        phases = {
            "output": "输出",
            "active": "输出",
            "interval": "路间间隔",
            "cycle_interval": "周期间隔",
            "cycle_wait": "周期等待",
            "stopped": "停止",
            "unknown": "等待状态",
            "waiting": "等待状态",
        }
        current = {}
        for group, caption in (("left", "A"), ("right", "B")):
            state = controllers.get(group) or {}
            channel = state.get("channel")
            if isinstance(channel, int) and not isinstance(channel, bool) and 1 <= channel <= 15:
                self._controller_positions[group] = channel
            cell = state.get("cell")
            active = (
                state.get("active")
                and isinstance(cell, int)
                and (1 <= cell <= 15 if group == "left" else 16 <= cell <= 30)
            )
            stage_hint = state.get("stage") or state.get("phase")
            known_idle = stage_hint in ("interval", "cycle_interval", "cycle_wait", "stopped")
            current[group] = str(cell) if active else "未接通" if known_idle else "待确认"
            profile = self.settings.get("controllers", {}).get(group, {})
            port = state.get("port") or profile.get("port") or "串口未配置"
            stage = state.get("stage") or state.get("phase")
            phase = phases.get(stage, "输出" if active else "未输出")
            elapsed = state.get("phase")
            if active and isinstance(elapsed, (int, float)) and math.isfinite(elapsed):
                phase = f"输出 {number(elapsed, 1)}s"
            status = STATE_NAMES.get(
                state.get("status"),
                {
                    "ok": "状态正常",
                    "valid": "状态正常",
                    "waiting": "等待状态",
                    "error": "状态异常",
                    "stale": "状态过期",
                    "invalid": "状态异常",
                    "unconfirmed": "等待确认",
                    "invalid_parameters": "参数不符",
                    "invalid_registers": "状态无效",
                    "invalid_time": "时序异常",
                    "slow_read": "读取超时",
                    "frozen": "倒计时停滞",
                    "transition": "通道切换",
                    "inconsistent": "状态不一致",
                    "read_error": "读取异常",
                }.get(state.get("status"), "等待状态"),
            )
            position = (
                f"当前小室 {cell:02d}"
                if active
                else "当前未接通"
                if known_idle
                else "接通状态待确认"
            )
            label = self.controller_labels[group]
            label.setText(f"{caption} 组 · {port} · {status} · {position} · {phase}")
            reason = str(state.get("reason") or "")
            if reason:
                label.setText(f"{label.text()} · {reason}")
            label.setToolTip(reason)
        progress = sum(self._controller_positions.values())
        self.scan_label.setText(f"本轮进度 {progress}/30")
        self.scan_label.setToolTip(
            f"当前接通 A：{current['left']}；B：{current['right']}\n"
            "进度为两组最近已知路号之和，表示扫描位置，不是有效样本数。"
        )
        self._set_measurement_indicators(controllers=controllers)
        if self.running:
            self.run_label.setText(
                "●  采集中"
                if any(s.get("active") for s in controllers.values())
                else "●  等待控制器"
            )

    def render(self, snapshot):
        """将只读快照投影到表格、指标与当前小室；不会因刷新频率改变统计结果。"""
        self.alarm_notifier.update(snapshot.get("alarms", []), notify=self.running)
        controller_snapshot = (
            snapshot.get("synchronization_source") == "controller" or "controllers" in snapshot
        )
        if controller_snapshot:
            controllers = snapshot.get("controllers") or {}
            self._render_controllers(controllers)
            active = {state.get("cell") for state in controllers.values() if state.get("active")}
        else:
            active = set(snapshot.get("cell_pair") or [])
            self._set_measurement_indicators(
                pair=snapshot.get("cell_pair") or [],
                synchronized=snapshot.get("synchronized", False),
            )
        for item in snapshot.get("cells", []):
            cell = item.get("cell")
            if not isinstance(cell, int) or not 1 <= cell <= 30:
                continue
            table = self.left_table if cell <= 15 else self.right_table
            row = (cell - 1) % 15
            age = item.get("age")
            tip = (
                f"距最近有效样本：{number(age, 1)} 秒\n累计 {item.get('count', 0)} 个有效样本"
                f"\n状态：{item.get('status', 'missing')}"
            )
            for col, key in ((1, "latest"), (2, "mean")):
                table.item(row, col).setText(number(item.get(key)))
                table.item(row, col).setForeground(
                    QColor("#ac751d" if item.get("status") in ("stale", "error") else "#25394e")
                )
                table.item(row, col).setToolTip(tip)
        for cell in range(1, 31):
            table = self.left_table if cell <= 15 else self.right_table
            row = (cell - 1) % 15
            for col in range(3):
                table.item(row, col).setBackground(
                    QColor("#dcf4f0") if cell in active else QColor("transparent")
                )
        metrics = snapshot.get("metrics", {})
        for key, label in self.metric_values.items():
            label.setText(number(metrics.get(key)))
        elapsed = max(0, int(snapshot.get("elapsed") or 0))
        self.elapsed_label.setText(
            f"实验时长  {elapsed // 3600:02d}:{elapsed // 60 % 60:02d}:{elapsed % 60:02d}"
        )
        pair = snapshot.get("cell_pair") or []
        if controller_snapshot:
            pass
        elif snapshot.get("synchronized") and len(pair) == 2:
            slot = snapshot.get("slot", pair[0] - 1)
            self.scan_label.setText(
                f"第 {snapshot.get('round', '—')} 轮   ·   本轮进度 {2 * (slot + 1)}/30   ·   组内 {number(snapshot.get('phase'), 1)} s"
            )
            self.scan_label.setToolTip(
                f"当前接通第 {pair[0]} 和 {pair[1]} 小室，两台电压表同时测量"
            )
        else:
            self.scan_label.setText("本轮进度 0/30 · 等待人工同步")
            self.scan_label.setToolTip("确认继电器当前接通第1和16小室后手动同步")
        for key in ("total_voltage", "power_kw"):
            self.metric_values[key].setToolTip(str(metrics.get("reason") or ""))

    def _device_port(self, role):
        if role.startswith("controller_"):
            return (
                self.settings.get("controllers", {})
                .get(role.removeprefix("controller_"), {})
                .get("port")
            )
        return self.settings.get("ports", {}).get(role, {}).get("port")

    def show_failure(self, message):
        """严重采集/保存异常也使用非模态通知，保证停止收尾可以继续。"""
        alarms = list(self.alarm_notifier.active.values())
        alarms.append(dict(key="acquisition_failure", level="error", message=message))
        self.alarm_notifier.update(alarms)

    def set_devices(self, statuses):
        """五路接口共用状态词和颜色，显示实际COM号；人工模式控制器显示未启用。"""
        self._device_statuses = dict(statuses)
        for role, label in self.device_labels.items():
            state = statuses.get(role, "stopped")
            if role.startswith("controller_") and not self._controller_mode():
                state = "disabled"
            elif self._stop_phase:
                # 晚到的连接状态包不能覆盖正在停止/保存的阶段提示。
                state = self._stop_phase
            color = {
                "connected": "#078575",
                "connecting": "#4b82ae",
                "disconnected": "#bd5555",
                "stopping": "#ac751d",
                "saving": "#ac751d",
            }.get(state, "#8a99a9")
            label.setText(f"●  {STATE_NAMES.get(state, '状态未知')}")
            label.setStyleSheet(f"color:{color};font-weight:600;font-size:12px;")
            caption = device_caption(role, self._device_port(role))
            self.device_names[role].setText(caption)
            label.setToolTip(f"{caption} · {STATE_NAMES.get(state, '状态未知')}")

    def append_event(self, wall_time, level, message):
        # 使用纯文本，设备返回的字符串不会作为 HTML 进入运行日志。
        """用纯文本追加有界运行日志，设备字符串不作为HTML执行或渲染。"""
        cursor = self.event_log.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        if self.event_log.toPlainText():
            cursor.insertBlock()
        cursor.insertText(f"{wall_time}  [{str(level).upper()}]  {message}")
        self.event_log.setTextCursor(cursor)
        self.event_log.ensureCursorVisible()

    def reset_session(self, mode):
        """清空本次显示缓存，保留由数据库管理的其他实验历史。"""
        self.alarm_notifier.reset()
        for role in DISPLAY_ROLES:
            self.device_names[role].setText(device_caption(role, self._device_port(role)))
        self._current_from_samples = False
        for table in (self.left_table, self.right_table):
            for row in range(15):
                for col in range(3):
                    table.item(row, col).setBackground(QColor("transparent"))
                    table.item(row, col).setToolTip("")
                    if col:
                        table.item(row, col).setText("—")
        for label in self.metric_values.values():
            label.setText("—")
        for chart in (*self.cell_charts.values(), *self.summary_charts.values()):
            chart.clear()
        self.elapsed_label.setText("实验时长  00:00:00")
        self._controller_positions = {"left": 0, "right": 0}
        if self._controller_mode():
            self._render_controllers({})
        else:
            self.scan_label.setText("本轮进度 0/30 · 待同步")
            self.scan_label.setToolTip("")
            self._set_measurement_indicators()
        self.event_log.clear()

    def add_sample(self, sample):
        """仅绘图有效样本，真实故障加入NaN断点，普通窗外等待不造采样点。"""
        role = sample.get("role")
        if role == "current":
            self._current_from_samples = True
            self.summary_charts["current"].append(
                sample.get("elapsed"),
                sample.get("value") if sample.get("valid") else None,
                sample.get("mean_value"),
            )
        if role in ("voltage_left", "voltage_right") and sample.get("valid"):
            cell = sample.get("cell")
            if cell in self.cell_charts:
                self.cell_charts[cell].append(
                    sample.get("elapsed"), sample.get("value"), sample.get("mean_value")
                )
        elif role in ("voltage_left", "voltage_right") and not sample.get("valid"):
            # 正常轮询等待不制造点；故障/证据缺失则留下一个断点，恢复后不跨故障连线。
            normal = {
                None,
                "outside_window",
                "out_of_window",
                "waiting",
                "unsynchronized",
                "unconfirmed",
            }
            if sample.get("status") in normal:
                return
            cell = sample.get("cell")
            candidates = (
                [cell]
                if cell in self.cell_charts
                else range(1, 16)
                if role == "voltage_left"
                else range(16, 31)
            )
            for number_ in candidates:
                chart = self.cell_charts[number_]
                if chart.latest_values and math.isfinite(chart.latest_values[-1]):
                    chart.append(sample.get("elapsed"), None, None)

    def add_summary(self, summary):
        """绘制引擎已经计算的每秒汇总，显示层不重新计算功率或累计均值。"""
        for key, mean_key in (
            ("total_voltage", "avg_total_voltage"),
            ("current", None),
            ("power_kw", "avg_power_kw"),
        ):
            if key == "current" and self._current_from_samples:
                continue
            self.summary_charts[key].append(
                summary.get("elapsed"),
                summary.get(key),
                summary.get(mean_key) if mean_key else None,
            )

    def closeEvent(self, event):
        # 正在采集时先拒绝关闭；控制器异步排空末尾数据后会再次发起安全关闭。
        """将关闭请求交给协调器，异步保存未完成时拒绝立即销毁窗口。"""
        if not self._closing:
            self._closing = True
            self.close_requested.emit()
        if getattr(self, "_close_blocked", False):
            self._closing = False
            event.ignore()
            return
        # 放大图是独立窗口；主窗口安全退出后也要关闭它们，释放整个进程。
        for chart in (*self.cell_charts.values(), *self.summary_charts.values()):
            if chart.detail_dialog is not None:
                chart.detail_dialog.close()
        event.accept()
