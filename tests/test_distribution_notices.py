"""发行包必须携带真实开源许可，不能仅依赖wheel内的商业许可提示。"""

import importlib.util
import hashlib
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collect_notices", ROOT / "scripts/collect_notices.py"
)
COLLECTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COLLECTOR)


def test_complete_offline_notices_and_provenance(tmp_path):
    destination = tmp_path / "licenses"
    COLLECTOR.collect(destination)
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in (
        destination / "upstream/qtbase/LICENSES/LGPL-3.0-only.txt"
    ).read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in (
        destination / "upstream/qtbase/LICENSES/GPL-3.0-only.txt"
    ).read_text(encoding="utf-8")
    assert "Copyright" in (destination / "upstream/pyserial/LICENSE.txt").read_text(
        encoding="utf-8"
    )
    assert "PYTHON SOFTWARE FOUNDATION LICENSE" in (destination / "Python/LICENSE.txt").read_text(
        encoding="utf-8"
    )
    assert 'id="openssl"' in (destination / "Python/第三方许可.html").read_text(encoding="utf-8")
    provenance = json.loads((destination / "upstream/SOURCES.json").read_text(encoding="utf-8"))
    assert provenance["files"] and all(row["sha256"] for row in provenance["files"])
    assert "_internal/PySide6" in (destination / "Qt维护与替换说明.txt").read_text(encoding="utf-8")


def test_upstream_notice_tampering_fails_closed(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "SOURCES.json").write_text(
        json.dumps({"files": [{"path": "LICENSE.txt", "sha256": "0" * 64}]}), encoding="utf-8"
    )
    (upstream / "LICENSE.txt").write_text("tampered", encoding="utf-8")
    monkeypatch.setattr(COLLECTOR, "UPSTREAM", upstream, raising=False)
    import pytest

    with pytest.raises(ValueError, match="SHA256"):
        COLLECTOR.collect(tmp_path / "output")


def test_installer_licenses_include_complete_matching_library_sources(tmp_path):
    destination = tmp_path / "licenses"
    COLLECTOR.collect(destination)
    # 许可摘录和上游链接不能替代实际随安装包交付的对应源码。
    sources = destination / "corresponding-source"
    assert sources.is_dir(), "发行包缺少Qt/PySide完整对应源码"
    manifest = json.loads((sources / "SOURCES.json").read_text(encoding="utf-8"))
    assert {row["name"] for row in manifest["archives"]} == {"qtbase", "qtsvg", "pyside-setup"}
    required_members = {
        "qtbase": "src/corelib/kernel/qobject.cpp",
        "qtsvg": "src/svg/qsvgrenderer.cpp",
        "pyside-setup": "sources/pyside6/PySide6/QtCore/typesystem_core.xml",
    }
    for row in manifest["archives"]:
        archive_path = sources / row["file"]
        assert hashlib.sha256(archive_path.read_bytes()).hexdigest().upper() == row["sha256"]
        with tarfile.open(archive_path) as archive:
            assert any(
                member.name.endswith("/" + required_members[row["name"]])
                for member in archive.getmembers()
            )
