"""通用异常必须留在会话历史，且异常停止不得给出成功文案。"""

import json
import sqlite3

import pytest

from cqu_monitor.controller import Controller
from test_controller import Window, default_settings


@pytest.mark.parametrize("asynchronous", [False, True])
def test_failure_root_cause_is_persisted_before_stop(qtbot, tmp_path, asynchronous):
    window = Window()
    c = Controller(window, default_settings(), tmp_path, launch_workers=False)
    c.start()

    class Reader:
        pending = []
        failure = None
        alive = True

        def is_alive(self):
            return self.alive

    reader = Reader()
    if asynchronous:
        c.launch_workers = True
        c.workers = [reader]
    root_cause = "计算核心异常：测试根因 root-42"
    try:
        c.fail(root_cause)
    finally:
        reader.alive = False
        qtbot.waitUntil(lambda: not c.running, timeout=4000)
    session = c.store.get_session(c.session_id)
    assert session["status"] == "error"
    with sqlite3.connect(c.store.db_path) as connection:
        events = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM events WHERE session_id=?", (c.session_id,)
            )
        ]
    assert any(root_cause in e["message"] and e["level"] == "error" for e in events)
    assert root_cause in c.last_error
    assert not any("本次实验记录已完整提交" in str(e) for e in window.events)
    assert c.recorder.pending_count == 0


@pytest.mark.parametrize("late_stage", ["finish_session", "after_recovery"])
def test_late_failure_is_saved_before_returning_to_standby(
    qtbot, tmp_path, monkeypatch, late_stage
):
    import threading

    window = Window()
    c = Controller(window, default_settings(), tmp_path, launch_workers=False)
    c.start()
    entered, release = threading.Event(), threading.Event()
    original = c.store.finish_session if late_stage == "finish_session" else c.save_unrecorded

    def gated(*args, **kwargs):
        if late_stage == "after_recovery":
            result = original(*args, **kwargs)
            entered.set()
            release.wait(3)
            return result
        entered.set()
        release.wait(3)
        return original(*args, **kwargs)

    if late_stage == "finish_session":
        monkeypatch.setattr(c.store, "finish_session", gated)
    else:
        monkeypatch.setattr(c, "save_unrecorded", gated)
    c.request_stop()
    qtbot.waitUntil(entered.is_set, timeout=3000)
    # 阶段保存已开始，原记录器可能已拒绝新的submit。
    c.launch_workers = True
    cause = "保存阶段新异常：late-root-73"
    try:
        c.fail(cause)
    finally:
        release.set()
        qtbot.waitUntil(lambda: not c.running, timeout=5000)
    assert c.store.get_session(c.session_id)["status"] == "error"
    with sqlite3.connect(c.store.db_path) as connection:
        events = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM events WHERE session_id=?", (c.session_id,)
            )
        ]
    assert any(cause in e["message"] for e in events)
    assert cause in c.last_error
    assert not any("本次实验记录已完整提交" in str(e) for e in window.events)
    assert not c.unrecorded


def test_late_error_transaction_rolls_back_if_event_insert_fails(tmp_path):
    from cqu_monitor.storage import SessionStore, StorageError

    store = SessionStore(tmp_path / "rollback.sqlite3")
    sid = store.create_session("hardware", default_settings(), "2026-10-17T00:00:00+08:00")
    store.finish_session(sid, None, "2026-10-17T00:01:00+08:00")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_error BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'test disk failure'); END"
        )
    with pytest.raises(StorageError):
        store.record_session_errors(
            sid,
            [
                dict(
                    session_id=sid,
                    wall_time="2026-10-17T00:01:00+08:00",
                    level="error",
                    message="must roll back",
                )
            ],
        )
    assert store.get_session(sid)["status"] == "completed"
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_new_error_during_diagnostic_commit_gets_another_commit(qtbot, tmp_path, monkeypatch):
    import threading

    c = Controller(Window(), default_settings(), tmp_path, launch_workers=False)
    c.start()
    finishing, finish_release, committing, commit_release = [threading.Event() for _ in range(4)]
    original_finish = c.store.finish_session
    original_errors = c.store.record_session_errors

    def slow_finish(*args, **kwargs):
        finishing.set()
        finish_release.wait(3)
        return original_finish(*args, **kwargs)

    def slow_errors(*args, **kwargs):
        committing.set()
        commit_release.wait(3)
        return original_errors(*args, **kwargs)

    monkeypatch.setattr(c.store, "finish_session", slow_finish)
    monkeypatch.setattr(c.store, "record_session_errors", slow_errors)
    c.request_stop()
    qtbot.waitUntil(finishing.is_set)
    c.launch_workers = True
    try:
        c.fail("first late error")
        finish_release.set()
        qtbot.waitUntil(committing.is_set)
        c.fail("second late error")
    finally:
        finish_release.set()
        commit_release.set()
        qtbot.waitUntil(lambda: not c.running, timeout=5000)
    with sqlite3.connect(c.store.db_path) as connection:
        messages = [
            json.loads(r[0])["message"] for r in connection.execute("SELECT payload FROM events")
        ]
    assert "first late error" in messages and "second late error" in messages
    assert c.store.get_session(c.session_id)["status"] == "error"
    assert "first late error" in c.last_error and "second late error" in c.last_error
    assert not c.unrecorded


def test_failed_late_error_commit_keeps_root_in_recovery(qtbot, tmp_path, monkeypatch):
    import threading

    c = Controller(Window(), default_settings(), tmp_path, launch_workers=False)
    c.start()
    entered, release = threading.Event(), threading.Event()
    original = c.store.finish_session

    def slow_finish(*args, **kwargs):
        entered.set()
        release.wait(3)
        return original(*args, **kwargs)

    def fail_commit(*args, **kwargs):
        raise OSError("test error database unavailable")

    monkeypatch.setattr(c.store, "finish_session", slow_finish)
    monkeypatch.setattr(c.store, "record_session_errors", fail_commit)
    c.request_stop()
    qtbot.waitUntil(entered.is_set)
    c.launch_workers = True
    cause = "root retained even if diagnostic disk write fails"
    try:
        c.fail(cause)
    finally:
        release.set()
        qtbot.waitUntil(lambda: not c.running, timeout=5000)
    assert c.recovery_path and c.recovery_path.exists()
    assert cause in c.recovery_path.read_text(encoding="utf-8")
    assert cause in c.last_error and "database unavailable" in c.last_error
    assert not any("本次实验记录已完整提交" in str(e) for e in c.window.events)


def test_undelivered_rewrite_failure_preserves_previous_complete_file(tmp_path, monkeypatch):
    import cqu_monitor.controller as module

    c = Controller(Window(), default_settings(), tmp_path, launch_workers=False)
    c.session_id = "atomic-recovery-case"
    c.unrecorded = [("event", dict(session_id=c.session_id, message="original root"))]
    c.save_unrecorded()
    previous = c.recovery_path.read_bytes()
    c.unrecorded.append(("event", dict(session_id=c.session_id, message="second root")))

    def failed_sync(_fd):
        raise OSError("test recovery disk failed")

    monkeypatch.setattr(module.os, "fsync", failed_sync)
    with pytest.raises(OSError):
        c.save_unrecorded()
    assert c.recovery_path.read_bytes() == previous
    assert len(c.unrecorded) == 2
