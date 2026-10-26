"""取得固定版本的完整对应源码及许可；正常离线构建只校验并复制本地文件。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tarfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "packaging/upstream_notices"
SOURCE_ARCHIVES = ROOT / "packaging/corresponding-source"
ARCHIVES = (
    (
        "qtbase",
        "https://download.qt.io/archive/qt/6.8/6.8.3/submodules/qtbase-everywhere-src-6.8.3.tar.xz",
        "qtbase-everywhere-src-6.8.3.tar.xz",
        "56001B905601BB9023D399F3BA780D7FA940F3E4861E496A7C490331F49E0B80",
    ),
    (
        "qtsvg",
        "https://download.qt.io/archive/qt/6.8/6.8.3/submodules/qtsvg-everywhere-src-6.8.3.tar.xz",
        "qtsvg-everywhere-src-6.8.3.tar.xz",
        "35EB516460F00F264EB504BAA253432384351CF23FB9980A5857190E8DEEF438",
    ),
    (
        "pyside-setup",
        "https://codeload.github.com/pyside/pyside-setup/tar.gz/refs/tags/v6.8.3",
        "pyside-setup-6.8.3.tar.gz",
        "055B467903327E947B60DDEFD2E9E66F6B7558B92A0728C4596E0DC8CE5DD695",
    ),
)


def sha(content):
    return hashlib.sha256(content).hexdigest().upper()


def fetch_archive(item):
    """完整源码与许可摘录共用同一经哈希确认的上游源包。"""
    name, url, filename, expected = item
    SOURCE_ARCHIVES.mkdir(parents=True, exist_ok=True)
    cache = SOURCE_ARCHIVES / filename
    if not cache.exists():
        temporary = cache.with_suffix(cache.suffix + ".part")
        try:
            with (
                urllib.request.urlopen(url, timeout=60) as response,
                temporary.open("wb") as output,
            ):
                while block := response.read(1024 * 1024):
                    output.write(block)
            with temporary.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest().upper() != expected:
                    raise ValueError(f"上游源包SHA256不匹配：{filename}")
            temporary.replace(cache)
        finally:
            temporary.unlink(missing_ok=True)
    with cache.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest().upper() != expected:
            raise ValueError(f"本地源包SHA256不匹配：{filename}")
    rows = []
    with tarfile.open(cache) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            relative = Path(*Path(member.name).parts[1:])
            filename = relative.name.lower()
            # 保留源码包的许可全文及第三方归属说明；这是保守超集，不充当运行时SBOM。
            if not (
                str(relative).startswith("LICENSES")
                or any(
                    word in filename
                    for word in ("license", "licence", "copying", "copyright", "notice")
                )
                or filename == "qt_attribution.json"
            ):
                continue
            if ".." in relative.parts or relative.is_absolute():
                raise ValueError("上游资料路径无效")
            content = archive.extractfile(member).read()
            target = DESTINATION / name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            rows.append(
                dict(
                    path=target.relative_to(DESTINATION).as_posix(),
                    sha256=sha(content),
                    source=url + "#" + member.name,
                )
            )
    print(f"{name}: {len(rows)} notice files", flush=True)
    return rows, dict(
        name=name,
        url=url,
        file=cache.name,
        sha256=expected,
        bytes=cache.stat().st_size,
        version="6.8.3",
    )


def main():
    DESTINATION.mkdir(parents=True, exist_ok=True)
    files, archives = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for rows, archive in pool.map(fetch_archive, ARCHIVES):
            files.extend(rows)
            archives.append(archive)
    url = "https://raw.githubusercontent.com/pyserial/pyserial/v3.5/LICENSE.txt"
    target = DESTINATION / "pyserial/LICENSE.txt"
    expected = "F91CB9813DE6A5B142B8F7F2DEDE630B5134160AEDAEAF55F4D6A7E2593CA3F3"
    # 已取得且哈希相符的固定版本资料可直接复用，网络波动不能损坏既有许可。
    content = target.read_bytes() if target.is_file() else b""
    if sha(content) != expected:
        with urllib.request.urlopen(url, timeout=30) as response:
            content = response.read()
        if sha(content) != expected:
            raise ValueError("pyserial 3.5许可证SHA256不匹配")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    files.append(dict(path="pyserial/LICENSE.txt", sha256=sha(content), source=url))
    (DESTINATION / "SOURCES.json").write_text(
        json.dumps(
            dict(
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                scope="Version-pinned upstream license and attribution files; conservative source-tree superset, not a runtime SBOM",
                archives=archives,
                files=files,
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (SOURCE_ARCHIVES / "SOURCES.json").write_text(
        json.dumps(dict(archives=archives), ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
