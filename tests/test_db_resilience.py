"""
Fail-closed behaviour for the security-critical reads in src/db.py (F-3).

The original contract returned [] / None for *both* "this group has nothing
configured" and "the database is unreachable". Callers cannot tell those apart,
so a transient outage looked exactly like "this user has no protection":
is_whitelisted() went False, get_group() went None, and ban_and_log's
`or "ban"` default then banned a group's own whitelisted admins.

The contract now is:
  - prefer a stale cached copy over reporting nothing
  - raise DatabaseUnavailable when nothing can be established
  - and check_user treats that as "don't act", never "nothing protects them"
"""
import asyncio

import pytest

from src import db
from src.utils import checker
from src.utils.checker import UserSnapshot, check_user


@pytest.fixture(autouse=True)
def _clear_caches():
    """These caches are module-level dicts; leaking them makes tests order-dependent."""
    for cache in (db._group_cache, db._whitelist_cache, db._kw_cache,
                  db._fp_cache, db._bad_actor_cache):
        cache.clear()
    yield
    for cache in (db._group_cache, db._whitelist_cache, db._kw_cache,
                  db._fp_cache, db._bad_actor_cache):
        cache.clear()


def _break_db(monkeypatch):
    """Simulate the pool being unable to hand out a connection."""
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: None)


ADMIN_ROW = {
    "user_id": 777, "username": "admin", "first_name": "Support",
    "last_name": "Team", "pfp_hash": None, "user_type": "admin", "is_bot": False,
}


def test_whitelist_read_failure_serves_stale_cache(monkeypatch):
    db._whitelist_cache[-100] = (0.0, [ADMIN_ROW])   # timestamp 0 => long expired
    _break_db(monkeypatch)
    assert db.get_whitelist(-100) == [ADMIN_ROW]


def test_whitelist_read_failure_with_cold_cache_raises(monkeypatch):
    _break_db(monkeypatch)
    with pytest.raises(db.DatabaseUnavailable):
        db.get_whitelist(-100)


def test_group_read_failure_with_cold_cache_raises(monkeypatch):
    _break_db(monkeypatch)
    with pytest.raises(db.DatabaseUnavailable):
        db.get_group(-100)


def test_is_whitelisted_keeps_protecting_from_stale_cache(monkeypatch):
    db._whitelist_cache[-100] = (0.0, [ADMIN_ROW])
    _break_db(monkeypatch)
    assert db.is_whitelisted(-100, 777) is True


def test_check_user_does_not_flag_when_whitelist_is_unavailable(monkeypatch):
    """
    The exact reported path: keyword cache warm, whitelist unavailable. Before
    the fix this returned flagged=True score=100 for a protected admin.
    """
    monkeypatch.setattr(checker, "is_whitelisted",
                        lambda gid, uid: (_ for _ in ()).throw(db.DatabaseUnavailable()))
    monkeypatch.setattr(checker, "is_false_positive", lambda gid, uid: False)
    monkeypatch.setattr(checker, "get_group", lambda gid: None)
    monkeypatch.setattr(checker, "get_known_bad_actor", lambda uid: None)
    monkeypatch.setattr(checker, "get_whitelist", lambda gid: [])
    monkeypatch.setattr(checker, "get_reserved_keywords",
                        lambda gid: [{"pattern": "support", "is_regex": False}])

    snap = UserSnapshot(user_id=777, username="admin",
                        first_name="Support", last_name="Team")
    res = asyncio.run(check_user(snap, -100))
    assert res.flagged is False
