"""曲线放大窗口只展示主图缓存，不另行采样或计算均值。"""

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


class ChartDetail(QDialog):
    """放大查看同一图表缓存，订阅实时更新；不创建另一条采集或统计链路。"""

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source
        self.setWindowTitle(f"{source.title} · 放大查看")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1100, 680)
        self.setMinimumSize(650, 420)
        body = QVBoxLayout(self)
        controls = QHBoxLayout()
        title = QLabel(f"{source.title} / {source.unit}")
        title.setStyleSheet("font-size:18px;font-weight:600;")
        controls.addWidget(title)
        controls.addStretch()
        reset = QPushButton("恢复视图")
        reset.clicked.connect(self.reset_view)
        controls.addWidget(reset)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        controls.addWidget(close)
        body.addLayout(controls)
        self.plot = pg.PlotWidget(background="white")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setLabel("bottom", "实验时间", units="s")
        self.plot.setLabel("left", source.title, units=source.unit)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.setMenuEnabled(False)
        self.latest_curve = self.plot.plot(pen=pg.mkPen("#098c82", width=2), connect="finite")
        self.mean_curve = self.plot.plot(
            pen=pg.mkPen("#d49935", width=1.6, style=Qt.PenStyle.DashLine), connect="finite"
        )
        body.addWidget(self.plot, 1)
        body.addWidget(
            QLabel("绿色：有效读数　金色虚线：累计平均　｜　滚轮缩放，按住鼠标拖动；新数据自动更新")
        )
        self.refresh()

    def refresh(self):
        """重绘来源图表的当前缓存，保留有效值与故障断点。"""
        self.latest_curve.setData(list(self.source.times), list(self.source.latest_values))
        self.mean_curve.setData(list(self.source.times), list(self.source.mean_values))

    def reset_view(self):
        """恢复自动范围以查看现有数据，不清空样本。"""
        self.plot.enableAutoRange()
        self.plot.autoRange()
