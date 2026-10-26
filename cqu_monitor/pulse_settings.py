"""两组脉冲控制器的只读连接设置，不在此创建串口客户端。"""

from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .config import default_controllers, validate_countdown_scale


def choice(options, value):
    widget = QComboBox()
    for label, data in options:
        widget.addItem(label, data)
    widget.setCurrentIndex(max(0, widget.findData(value)))
    return widget


def controller_page(settings):
    """保留两组独立串口参数；同物理总线的约束交给统一配置校验。"""
    page = QWidget()
    page.setObjectName("controllerSettings")
    page.setStyleSheet("QWidget#controllerSettings {background:#eff3f8;}")
    body = QVBoxLayout(page)
    controls = {}
    profiles = default_controllers()
    for group, caption in (("left", "A 组 · 小室 01—15"), ("right", "B 组 · 小室 16—30")):
        profile = {**profiles[group], **settings.get("controllers", {}).get(group, {})}
        # 旧设置含不支持的精度时明确拒绝，不让下拉框静默替换设备解释。
        validate_countdown_scale(profile["countdown_scale"])
        title = QLabel(caption)
        title.setStyleSheet("font-weight:700;color:#183b54;")
        body.addWidget(title)
        grid = QGridLayout()
        port = QComboBox()
        port.setEditable(True)
        port.setCurrentText(profile["port"])
        unit = QSpinBox()
        unit.setRange(1, 99)
        unit.setValue(profile["unit"])
        timeout = QDoubleSpinBox()
        timeout.setRange(0.02, 0.2)
        timeout.setDecimals(2)
        timeout.setSingleStep(0.05)
        timeout.setValue(profile["timeout"])
        items = {
            "port": port,
            "baudrate": choice(
                [(str(n), n) for n in (4800, 9600, 19200, 38400, 57600, 115200)],
                profile["baudrate"],
            ),
            "unit": unit,
            "parity": choice(
                [("偶校验 E", "E"), ("无校验 N", "N"), ("奇校验 O", "O")], profile["parity"]
            ),
            "stopbits": choice([("1", 1), ("2", 2)], profile["stopbits"]),
            "timeout": timeout,
            "function": choice(
                [("03 · 保持寄存器", 3), ("04 · 输入寄存器", 4)], profile["function"]
            ),
            "address_offset": choice(
                [("手册地址直接使用", 0), ("手册地址减 1", -1)], profile["address_offset"]
            ),
            "countdown_scale": choice(
                [("0.01 秒", 0.01), ("0.1 秒", 0.1)], profile["countdown_scale"]
            ),
        }
        captions = (
            "串口",
            "波特率",
            "站号",
            "校验",
            "停止位",
            "超时 / s",
            "高级 · 读取功能",
            "高级 · 地址解释",
            "高级 · 倒计时单位",
        )
        for index, ((key, widget), label) in enumerate(zip(items.items(), captions)):
            row, col = divmod(index, 3)
            grid.addWidget(QLabel(label), row * 2, col)
            grid.addWidget(widget, row * 2 + 1, col)
        controls[group] = items
        body.addLayout(grid)
    note = QLabel(
        "仅读取状态，不向控制器发送修改命令。输出宽度须与“采样时间”中的设置一致，路数须为15；"
        "电压只采用设置的有效时间范围，电流连续采集。高级选项须与设备手册一致。"
    )
    note.setWordWrap(True)
    note.setObjectName("subtitle")
    body.addWidget(note)
    body.addStretch()
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(page)
    return scroll, controls


def controller_values(controls):
    return {
        group: {
            key: widget.currentText().strip().upper()
            if key == "port"
            else widget.value()
            if key in ("unit", "timeout")
            else widget.currentData()
            for key, widget in items.items()
        }
        for group, items in controls.items()
    }
