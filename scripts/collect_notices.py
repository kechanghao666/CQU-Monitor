"""随独立程序保留实际分发依赖的版本、许可证与版权文件。"""

from importlib import metadata
import hashlib
import json
from pathlib import Path
import shutil
import sys

UPSTREAM = Path(__file__).resolve().parents[1] / "packaging/upstream_notices"
SOURCE_ARCHIVES = UPSTREAM.parent / "corresponding-source"
RUNTIME = (
    "PySide6",
    "PySide6_Essentials",
    "PySide6_Addons",
    "shiboken6",
    "pyqtgraph",
    "numpy",
    "pymodbus",
    "pyserial",
    "six",
    "openpyxl",
    "et_xmlfile",
    "pyinstaller",
)


def collect(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    sources = json.loads((UPSTREAM / "SOURCES.json").read_text(encoding="utf-8"))
    # 离线构建先核对上游文件完整性，缺件或被改写时停止，不能出具不完整的许可目录。
    for row in sources["files"]:
        source = UPSTREAM / row["path"]
        if (
            not source.is_file()
            or hashlib.sha256(source.read_bytes()).hexdigest().upper() != row["sha256"]
        ):
            raise ValueError(f"上游许可证SHA256不匹配：{row['path']}")
    # 安装程序本身也交付对应源码；只有许可摘录或上游链接并不等于交付源码。
    manifest = json.loads((SOURCE_ARCHIVES / "SOURCES.json").read_text(encoding="utf-8"))
    if manifest["archives"] != sources["archives"]:
        raise ValueError("对应源码清单与上游许可来源不一致")
    for row in manifest["archives"]:
        source = SOURCE_ARCHIVES / row["file"]
        with source.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest().upper() != row["sha256"]:
                raise ValueError(f"对应源码SHA256不匹配：{row['file']}")
    shutil.copytree(SOURCE_ARCHIVES, destination / "corresponding-source", dirs_exist_ok=True)
    shutil.copytree(UPSTREAM, destination / "upstream", dirs_exist_ok=True)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise FileNotFoundError(f"缺少Python完整许可证：{python_license}")
    (destination / "Python").mkdir(exist_ok=True)
    shutil.copy2(python_license, destination / "Python/LICENSE.txt")
    python_third_party = Path(sys.base_prefix) / "Doc/html/license.html"
    if not python_third_party.is_file():
        raise FileNotFoundError(f"缺少当前Python安装的第三方许可：{python_third_party}")
    shutil.copy2(python_third_party, destination / "Python/第三方许可.html")
    (destination / "Python/SOURCES.json").write_text(
        json.dumps(
            dict(
                version=sys.version,
                files=[
                    dict(source=str(p), sha256=hashlib.sha256(p.read_bytes()).hexdigest().upper())
                    for p in (python_license, python_third_party)
                ],
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    shutil.copy2(UPSTREAM.parent / "Qt维护与替换说明.txt", destination / "Qt维护与替换说明.txt")
    shutil.copy2(
        UPSTREAM.parent / "软件许可与资料来源说明.txt", destination / "软件许可与资料来源说明.txt"
    )
    rows = []
    for name in RUNTIME:
        distribution = metadata.distribution(name)
        rows.append(f"{distribution.metadata['Name']}=={distribution.version}")
        package_dir = destination / name
        package_dir.mkdir(exist_ok=True)
        (package_dir / "METADATA.txt").write_text(
            distribution.read_text("METADATA") or "", encoding="utf-8"
        )
        for file in distribution.files or []:
            if any(
                token in str(file).upper()
                for token in ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")
            ):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    # 保留包内相对结构，避免各依赖同名LICENSE覆盖。
                    target = destination / name / str(file).replace("..", "_")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    (destination / "VERSIONS.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    collect(sys.argv[1])
