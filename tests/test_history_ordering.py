"""历史的先后顺序由实际时刻决定，不受ISO字符串中的时区偏移影响。"""

from cqu_monitor.storage import SessionStore


def test_sessions_are_newest_first_across_timezones(tmp_path):
    store = SessionStore(tmp_path / "history.sqlite3")
    # +08:00的10点实际早于UTC的9点；单纯比较文本会把这两次实验排反。
    older = store.create_session("hardware", {}, "2026-10-18T10:00:00+08:00")
    newer = store.create_session("hardware", {}, "2026-10-18T09:00:00+00:00")
    assert [row["id"] for row in store.list_sessions()] == [newer, older]


def test_equivalent_session_times_keep_newer_insertion_first(tmp_path):
    store = SessionStore(tmp_path / "history.sqlite3")
    first = store.create_session("hardware", {}, "2026-10-18T10:00:00+08:00")
    second = store.create_session("hardware", {}, "2026-10-18T02:00:00Z")
    assert [row["id"] for row in store.list_sessions()] == [second, first]
