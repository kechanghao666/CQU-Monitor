"""本机新装、升级、卸载保留数据验收；不连接设备，不向正式data生成测量记录。

必须显式给出正式目录。沙盒安装会暂时变更当前用户的卸载登记，正常完成时
最后升级正式目录以恢复登记；中途失败保留证据，应排除错误后重新完成验收。
"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]
INSPECT = runpy.run_path(str(ROOT / "scripts/read_database_evidence.py"))["inspect"]
VERSION = runpy.run_path(str(ROOT / "cqu_monitor/__init__.py"))["__version__"]


def sha(path):
    """分块计算文件指纹，避免把大源包全部读入内存。"""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest().upper()


def files(directory, data=False):
    """SQLite共享页和空WAL是瞬态文件；非空WAL仍属于需保全的数据。"""
    result = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if data and (
            path.name in ("measurements.sqlite3-shm", "application.lock")
            or (path.name == "measurements.sqlite3-wal" and path.stat().st_size == 0)
        ):
            continue
        result[path.relative_to(directory).as_posix()] = sha(path)
    return result


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(arguments, timeout=180):
    """使用程序的公开命令行入口并等待真实退出，不发送窗口控制消息。"""
    completed = subprocess.run(
        [str(value) for value in arguments],
        timeout=timeout,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    require(completed.returncode == 0, f"子进程退出失败({completed.returncode})：{arguments[0]}")


def install(setup, bundle, destination, output, label):
    """静默安装后逐文件核对，再运行安装版无设备启动及退出检查。"""
    run(
        [
            setup,
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            f"/DIR={destination}",
            f"/LOG={output / (label + '.log')}",
        ]
    )
    expected = files(bundle)
    require(
        all(
            (destination / name).is_file() and sha(destination / name) == digest
            for name, digest in expected.items()
        ),
        "安装文件与构建bundle不一致",
    )
    require(
        files(bundle / "_internal") == files(destination / "_internal"), "旧运行库残留或依赖缺失"
    )
    require(
        files(bundle / "licenses") == files(destination / "licenses"), "安装后的完整许可/源码不一致"
    )
    startup = output / (label + "_startup")
    run([destination / "CQU-Monitor.exe", "--check-startup", "--output", startup], timeout=60)
    report = json.loads((startup / "report.json").read_text(encoding="utf-8-sig"))
    require(
        report["passed"] and report["frozen"] and report["version"] == VERSION,
        "安装版启动/版本不符",
    )
    require(
        report["ports_opened"] == report["samples_generated"] == 0, "无设备检查打开端口或产生读数"
    )
    return dict(
        bundle_files=len(expected), startup_passed=True, startup_report=str(startup / "report.json")
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--formal-dir", type=Path, required=True, help="已有的正式安装目录，验收最后对其升级"
    )
    parser.add_argument("--output", type=Path, required=True, help="必须是未存在的验收目录")
    args = parser.parse_args()
    setup, bundle, formal, output = (
        getattr(args, name).resolve() for name in ("installer", "bundle", "formal_dir", "output")
    )
    output.mkdir(parents=True, exist_ok=False)
    report = dict(
        passed=False,
        version=VERSION,
        checked_at=datetime.now().astimezone().isoformat(),
        realHardwareTested=False,
        continuous24hTested=False,
        cleanOsTested=False,
        installedMouseInteractionTested=False,
    )
    try:
        require(
            setup.is_file() and (bundle / "CQU-Monitor.exe").is_file(), "安装包或构建bundle缺失"
        )
        require(
            (formal / "CQU-Monitor.exe").is_file(), "正式目录不是已有安装，不能对未知位置执行升级"
        )
        require(not (bundle / "data").exists(), "构建bundle含data，拒绝分发或覆盖用户数据")
        require(
            not output.is_relative_to(formal) and not formal.is_relative_to(output),
            "验收目录与正式目录须彼此独立",
        )
        listing = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq CQU-Monitor.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        require(
            not any(
                line.lower().startswith('"cqu-monitor.exe"') for line in listing.stdout.splitlines()
            ),
            "请先正常退出CQU-Monitor，再运行安装验收",
        )
        before_database = INSPECT(formal / "data/measurements.sqlite3")
        require(
            not before_database["active_sessions"],
            "正式库含未结束会话，先核对恢复，不能直接升级验收",
        )
        before_files = files(formal / "data", data=True)
        # 此时已确认没有运行实例，保全整data；验收结束确认无改动后可删除临时备份。
        shutil.copytree(formal / "data", output / "before-data")
        (output / "before-database.json").write_text(
            json.dumps(before_database, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        sandbox = output / "installed-sandbox"
        require(sandbox.is_relative_to(output), "沙盒位置超出验收目录")
        fresh = install(setup, bundle, sandbox, output, "sandbox_fresh")
        data = sandbox / "data"
        data.mkdir(exist_ok=True)
        (data / "acceptance-sentinel.txt").write_text(
            "仅用于安装/卸载的数据保留检查", encoding="utf-8"
        )
        (data / "settings.json").write_text('{"acceptance_only":true}', encoding="utf-8")
        preserved = files(data)
        upgrade = install(setup, bundle, sandbox, output, "sandbox_upgrade")
        require(files(data) == preserved, "沙盒覆盖安装修改了用户data")
        run(
            [
                sandbox / "unins000.exe",
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                f"/LOG={output / 'sandbox_uninstall.log'}",
            ]
        )
        deadline = time.monotonic() + 15
        while (sandbox / "CQU-Monitor.exe").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        require(not (sandbox / "CQU-Monitor.exe").exists(), "卸载后程序仍存在")
        require(files(data) == preserved, "卸载未完整保留data")
        report.update(
            sandboxInstall=True,
            sandboxUpgrade=True,
            sandboxUninstall=True,
            uninstallPreservedData=True,
            fresh=fresh,
            upgrade=upgrade,
        )

        # 沙盒已验证后才升级正式目录；该操作同时把卸载登记恢复到正式位置。
        installed = install(setup, bundle, formal, output, "formal_upgrade")
        after_database = INSPECT(formal / "data/measurements.sqlite3")
        require(before_database["tables"] == after_database["tables"], "正式数据库内容发生变化")
        require(
            before_database["schema_version"] == after_database["schema_version"],
            "数据库版本意外改变",
        )
        require(before_files == files(formal / "data", data=True), "正式data或设置未原样保留")
        (output / "after-database.json").write_text(
            json.dumps(after_database, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report.update(
            passed=True,
            actualUpgrade=True,
            dataPreserved=True,
            settingsPreserved=True,
            internalLibrariesMatch=True,
            frozenStartupPassed=True,
            formalDirectory=str(formal),
            installerSHA256=sha(setup),
            formal=installed,
            tableFingerprints=after_database["tables"],
        )
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    (output / "installation-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
