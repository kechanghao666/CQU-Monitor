"""保留官方绑定支持，去掉本应用不使用的可选软件OpenGL二进制。"""

from pathlib import Path

from PyInstaller.utils.hooks import check_requirement
from PyInstaller.utils.hooks.qt import ensure_single_qt_bindings_package, pyside6_library_info


ensure_single_qt_bindings_package("PySide6")
if pyside6_library_info.version is not None:
    hiddenimports = ["shiboken6", "inspect"]
    if check_requirement("PySide6 >= 6.4.0"):
        hiddenimports.append("PySide6.support.deprecated")
    # 当前图表采用Qt默认光栅绘图，不开启OpenGL；无需分发额外Mesa软件渲染器。
    binaries = [
        item
        for item in pyside6_library_info.collect_extra_binaries()
        if Path(item[0]).name.lower() != "opengl32sw.dll"
    ]
