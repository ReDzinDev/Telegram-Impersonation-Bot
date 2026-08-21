"""
A capped sweep must resume where it stopped (R-4).

On hitting SWEEP_HARD_CAP_SECONDS the loop broke and reported partial=True, but
no position was persisted — the next run restarted from the first member. Since
participant ordering is stable, the same prefix was re-scanned forever and the
tail was NEVER scanned, while /sweep told the admin "Re-run /sweep to continue".

Not an edge case: with reserved keywords, every unflagged member costs one paced
GetFullUser at BIO_FETCH_MIN_INTERVAL, so a 7200s cap tops out around 6,000
members — far fewer once the interval ratchets.

Also covered: one bad member must not abort the remaining ones, and an exception
mid-iteration must mark the run partial rather than recording a clean sweep.
"""
import asyncio

import pytest

from src.utils.checker import DetectionResult
from src.watcher import sweep as sweep_mod


class _User:
    def __init__(self, uid, is_bot=False, deleted=False):
        self.id = uid
        self.is_bot = is_bot
        self.is_deleted = deleted
        self.username = f"u{uid}"
        self.first_name = f"User{uid}"
        self.last_name = None


class _Member:
    def __init__(self, uid):
        self.user = _User(uid)
        self.status = "member"


class _FakePyro:
    def __init__(self, member_count, explode_at=None):
        self.member_count = member_count
        self.explode_at = explode_at
        self.yielded = []

    async def get_chat(self, chat_id):
        return object()

    async def get_chat_members(self, chat_id):
        for i in range(self.member_count):
            if self.explode_at is not None and i == self.explode_at:
                raise RuntimeError("enumeration blew up")
            self.yielded.append(i)
            yield _Member(1000 + i)


class _FakeBot:
    id = 999


@pytest.fixture
def sweep_env(monkeypatch):
    """Neutralise everything except the loop logic under test."""
    state = {"offset_writes": [], "sweep_runs": [], "seen": []}

    async def fake_run_db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(sweep_mod, "run_db", fake_run_db)
    monkeypatch.setattr(sweep_mod, "get_reserved_keywords", lambda gid: [])
    monkeypatch.setattr(sweep_mod, "is_whitelisted", lambda gid, uid: False)
    monkeypatch.setattr(sweep_mod, "mark_seen",
                        lambda gid, uid: state["seen"].append(uid))
    monkeypatch.setattr(sweep_mod, "get_whitelist", lambda gid: [])
    monkeypatch.setattr(sweep_mod, "upsert_whitelisted_user", lambda **kw: True)
    monkeypatch.setattr(
        sweep_mod, "record_sweep_run",
        lambda *a, **k: state["sweep_runs"].append((a, k)),
    )
    monkeypatch.setattr(
        sweep_mod, "set_group_sweep_offset",
        lambda gid, offset: state["offset_writes"].append(offset), raising=False,
    )
    monkeypatch.setattr(sweep_mod, "get_group_sweep_offset",
                        lambda gid: state.get("start_offset", 0), raising=False)

    async def clean(snapshot, group_id):
        return DetectionResult(flagged=False)
    monkeypatch.setattr(sweep_mod, "check_user", clean)

    async def no_pfp(pyro, uid, wait=False):
        return None
    monkeypatch.setattr(sweep_mod, "_fetch_pfp", no_pfp)

    async def never(**kw):
        raise AssertionError("ban_and_log should not run for clean members")
    monkeypatch.setattr(sweep_mod, "ban_and_log", never)

    monkeypatch.setattr(sweep_mod, "refresh_whitelist_pfps",
                        lambda *a, **k: asyncio.sleep(0), raising=False)
    return state


def _run(pyro, gid=-100):
    return asyncio.run(sweep_mod.sweep_group(pyro, _FakeBot(), gid))


def test_completed_sweep_resets_the_cursor(sweep_env):
    result = _run(_FakePyro(5))
    assert result["partial"] is False
    assert sweep_env["offset_writes"][-1] == 0, "a full pass must clear the cursor"


def test_capped_sweep_records_where_it_stopped(sweep_env, monkeypatch):
    monkeypatch.setattr(sweep_mod, "SWEEP_HARD_CAP_SECONDS", 0)   # cap immediately
    result = _run(_FakePyro(50))
    assert result["partial"] is True
    assert sweep_env["offset_writes"], "no resume point was persisted"
    assert sweep_env["offset_writes"][-1] > 0


def test_a_stored_offset_skips_already_scanned_members(sweep_env):
    sweep_env["start_offset"] = 3
    pyro = _FakePyro(5)
    result = _run(pyro)
    # Members 0-2 are skipped without being checked; 3 and 4 are scanned.
    assert result["checked"] == 2, f"expected 2 scanned, got {result['checked']}"
    assert sweep_env["offset_writes"][-1] == 0


def test_one_bad_member_does_not_abort_the_rest(sweep_env, monkeypatch):
    calls = {"n": 0}

    async def flaky(snapshot, group_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("pathological avatar")
        return DetectionResult(flagged=False)
    monkeypatch.setattr(sweep_mod, "check_user", flaky)

    result = _run(_FakePyro(5))
    assert result["errors"] == 1
    assert calls["n"] == 5, "iteration stopped at the bad member"


def test_an_exception_mid_iteration_marks_the_run_partial(sweep_env):
    result = _run(_FakePyro(10, explode_at=4))
    assert result["partial"] is True, (
        "an interrupted sweep was recorded as a clean, complete pass"
    )
