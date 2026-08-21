"""
Config setters must report whether the write actually landed (B-4).

Every one of these ran `UPDATE groups SET ... WHERE group_id = %s` and returned
True unconditionally, so rowcount == 0 (no such group) was reported as success —
and every call site discarded the boolean and printed a checkmark anyway.

The failure that matters: an admin switches a group to `alert` during a database
blip, is told it worked, and detection keeps banning on the old config.
/whitelist was already fixed this way ("Don't tell the admin someone is
protected when the write failed"); the config setters hadn't caught up.
"""
import pytest

from src import db


class _FakeCursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, rowcount=1):
        self._rowcount = rowcount
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return _FakeCursor(self._rowcount)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


@pytest.fixture
def fake_db(monkeypatch):
    """Swap the pool for a fake whose rowcount the test controls."""
    holder = {}

    def install(rowcount):
        conn = _FakeConn(rowcount)
        holder["conn"] = conn
        monkeypatch.setattr(db, "get_connection", lambda *a, **k: conn)
        monkeypatch.setattr(db, "put_connection", lambda c: None)
        for cache in (db._group_cache, db._whitelist_cache, db._kw_cache,
                      db._fp_cache, db._bad_actor_cache):
            cache.clear()
        return conn

    return install


SETTERS = [
    ("set_group_log_channel",  (-100, 12345)),
    ("set_group_action_mode",  (-100, "alert")),
    ("set_group_threshold",    (-100, 90)),
    ("set_group_score_bands",  (-100, 90, 78)),
    ("set_group_blocklist",    (-100, False)),
    ("set_group_thresholds",   (-100, 88, 85)),
]


@pytest.mark.parametrize("name,args", SETTERS)
def test_setter_reports_failure_when_no_row_was_updated(fake_db, name, args):
    fake_db(rowcount=0)
    assert getattr(db, name)(*args) is False, f"{name} claimed success on 0 rows"


@pytest.mark.parametrize("name,args", SETTERS)
def test_setter_reports_success_when_a_row_was_updated(fake_db, name, args):
    fake_db(rowcount=1)
    assert getattr(db, name)(*args) is True, f"{name} reported failure on a real write"


@pytest.mark.parametrize("name,args", SETTERS)
def test_setter_returns_false_when_no_connection(monkeypatch, name, args):
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: None)
    assert getattr(db, name)(*args) is False


def test_mark_false_positive_reports_whether_it_persisted(fake_db):
    """It returned None, so no caller could check it."""
    fake_db(rowcount=1)
    assert db.mark_false_positive(-100, 555, cleared_by=1) is True
    fake_db(rowcount=0)
    assert db.mark_false_positive(-100, 555, cleared_by=1) is False
