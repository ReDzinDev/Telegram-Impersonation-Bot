"""
Retention must run whether or not a global log channel is configured (R-7).

purge_old_records was reachable from exactly one place — inside
run_daily_summary — and main() only creates that task `if LOG_CHANNEL_ID`. So on
a deployment where every group sets its own /setlogchannel and no global channel
exists (a perfectly ordinary configuration, and the one B-2 was about), retention
NEVER RAN. logs, sweep_runs, name_change_log and false_positives grew forever on
a Hobby-tier disk.

Two tables were outside the purge entirely: seen_members, which gets a row per
(group, user) forever and is never deleted except by unmark_seen, and
admin_actions, an append-only audit log. seen_members is also the table whose
missing index made get_watched_groups_for_user a full scan on every raw update,
so unbounded growth there costs more than disk.

All the purge predicates were unindexed, so each pass was a sequential scan:
logs.created_at cannot use idx_logs_group(group_id, created_at DESC).
"""
import re
from pathlib import Path

import pytest

from src import db

ROOT = Path(__file__).resolve().parent.parent


class _FakeCursor:
    def __init__(self, log):
        self.rowcount = 3
        self._log = log

    def execute(self, sql, params=None):
        self._log.append(" ".join(sql.split()))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self.statements = []
        self.committed = False

    def cursor(self):
        return _FakeCursor(self.statements)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


@pytest.fixture
def fake_conn(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: conn)
    monkeypatch.setattr(db, "put_connection", lambda c: None)
    return conn


PURGED_TABLES = [
    "logs",
    "sweep_runs",
    "name_change_log",
    "false_positives",
    "seen_members",
    "admin_actions",
]


@pytest.mark.parametrize("table", PURGED_TABLES)
def test_every_growing_table_is_purged(fake_conn, table):
    db.purge_old_records()
    deletes = [s for s in fake_conn.statements if s.startswith("DELETE FROM")]
    assert any(f"DELETE FROM {table}" in s for s in deletes), (
        f"{table} is never purged; it grows forever"
    )


def test_the_result_reports_every_table(fake_conn):
    result = db.purge_old_records()
    for table in PURGED_TABLES:
        assert table in result, f"{table} missing from the purge report"


def test_retention_windows_are_configurable(fake_conn):
    db.purge_old_records(logs_days=7, sweeps_days=14, seen_days=30, actions_days=365)
    assert fake_conn.committed


def test_no_connection_is_survivable(monkeypatch):
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: None)
    result = db.purge_old_records()
    assert all(v == 0 for v in result.values())


# ── it has to actually be scheduled ───────────────────────────────────────────

def test_retention_has_its_own_background_task():
    """Not buried inside the daily summary, whose task is conditional."""
    assert hasattr(db, "purge_old_records")
    from src import main

    assert hasattr(main, "_retention_loop"), (
        "retention needs its own loop, independent of the summary task"
    )


def test_main_schedules_retention_unconditionally():
    """
    A source-level guard. The bug was not in purge_old_records — it was that
    nothing called it unless LOG_CHANNEL_ID happened to be set.
    """
    source = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    body = source[source.index("async def main("):]
    match = re.search(r"^(\s*)retention_task = asyncio\.create_task\(", body, re.M)
    assert match, "main() never creates a retention task"
    # Four spaces = directly in main()'s body, not nested under an `if`.
    assert len(match.group(1)) == 4, (
        "the retention task is created inside a conditional block"
    )


def test_summary_no_longer_owns_retention():
    source = (ROOT / "src" / "watcher" / "summary.py").read_text(encoding="utf-8")
    assert "purge_old_records" not in source, (
        "retention is still coupled to the summary task"
    )


# ── the purge predicates need indexes ─────────────────────────────────────────

@pytest.mark.parametrize("index_on", [
    ("logs", "created_at"),
    ("sweep_runs", "created_at"),
    ("name_change_log", "changed_at"),
    ("false_positives", "expires_at"),
    ("seen_members", "last_checked_at"),
    ("admin_actions", "created_at"),
])
def test_purge_predicates_are_indexed(index_on):
    table, column = index_on
    ddl = (ROOT / "src" / "db.py").read_text(encoding="utf-8")
    pattern = re.compile(
        rf"CREATE INDEX IF NOT EXISTS \w+ ON {table}\s*\(\s*{column}\b", re.I
    )
    assert pattern.search(ddl), (
        f"{table}({column}) drives a purge DELETE but has no index — each pass "
        "is a sequential scan"
    )


# ── record_sweep_run must actually write the caveats ──────────────────────────

class _ParamCursor:
    def __init__(self, calls):
        self.rowcount = 1
        self._calls = calls

    def execute(self, sql, params=None):
        self._calls.append((" ".join(sql.split()), params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _ParamConn:
    def __init__(self):
        self.calls = []

    def cursor(self):
        return _ParamCursor(self.calls)

    def commit(self):
        pass

    def rollback(self):
        pass


def test_sweep_run_parameters_include_the_caveats(monkeypatch):
    """
    Guards the SQL itself, not just the call site. A test that only checks what
    sweep_group PASSES cannot notice the writer dropping the values on the floor
    between the signature and the INSERT.
    """
    conn = _ParamConn()
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: conn)
    monkeypatch.setattr(db, "put_connection", lambda c: None)

    db.record_sweep_run(
        -100, iterated=50, checked=40, flagged=2, errors=1, trigger="auto",
        partial=True, bios_skipped=7, pfps_skipped=3,
    )
    assert conn.calls, "nothing was executed"
    sql, params = conn.calls[-1]
    assert "partial" in sql and "bios_skipped" in sql and "pfps_skipped" in sql
    assert True in params, "partial=True never reached the statement"
    assert 7 in params, "bios_skipped never reached the statement"
    assert 3 in params, "pfps_skipped never reached the statement"
