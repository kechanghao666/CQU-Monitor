"""在依赖扫描前限制插件，避免未使用的PDF/虚拟键盘引入额外Qt模块。"""

from pathlib import Path

from PyInstaller.utils.hooks.qt import add_qt6_dependencies


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)


def required_plugin(item):
    path = Path(item[0])
    category = path.parent.name.lower()
    if category == "imageformats":
        # PNG由QtGui内建；JPEG/GIF/ICO/SVG保留，覆盖窗口图标及常用图像。
        return path.name.lower() in {"qjpeg.dll", "qgif.dll", "qico.dll", "qsvg.dll"}
    if category == "platforms":
        return path.name.lower() in {"qwindows.dll", "qoffscreen.dll"}
    if category in {"platforminputcontexts", "generic"}:
        return False
    return True


binaries = [item for item in binaries if required_plugin(item)]
