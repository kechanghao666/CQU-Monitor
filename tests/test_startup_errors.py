"""安装目录不可写或日志不能建立时，启动失败须有可见原因。"""

from pathlib import Path

import pytest

from cqu_monitor import __main__ as entry


@pytest.mark.parametrize("failure", ["data_directory", "log_file"])
def test_startup_io_failure_is_reported_to_user(qapp, tmp_path, monkeypatch, failure):
    target = tmp_path / "data"
    messages = []
    monkeypatch.setattr(entry.sys, "argv", ["CQU-Monitor", "--data-dir", str(target)])
    # 使用真实QApplication，复用pytest-qt持有的唯一实例。
    monkeypatch.setattr(entry, "QApplication", lambda _argv: qapp)
    monkeypatch.setattr(entry.QMessageBox, "critical", lambda *args: messages.append(args[-1]))
    if failure == "data_directory":
        original = Path.mkdir

        def denied(path, *args, **kwargs):
            if path == target:
                raise PermissionError("测试：数据目录不可写")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", denied)
    else:

        def denied_log(*args, **kwargs):
            raise OSError("测试：无法创建日志文件")

        monkeypatch.setattr(entry, "RotatingFileHandler", denied_log)
    assert entry.main() == 1
    assert len(messages) == 1 and "测试：" in messages[0]
