"""
Tests for the detection decision logic in src/utils/checker.py.

check_user / ban_and_log touch the DB, so we monkeypatch the db-backed
names bound into the checker module. Async functions are driven with
asyncio.run() so no pytest-asyncio plugin is required.
"""
import asyncio

import pytest

from src.utils import checker
from src.utils.checker import UserSnapshot, check_user, ban_and_log


def _patch_db(monkeypatch, *, group=None, whitelist=None, keywords=None,
              bad_actor=None, whitelisted_ids=()):
    """Wire up the db functions checker imports, with sensible defaults."""
    monkeypatch.setattr(checker, "get_group", lambda gid: group)
    monkeypatch.setattr(checker, "get_whitelist", lambda gid: whitelist or [])
    monkeypatch.setattr(checker, "get_reserved_keywords", lambda gid: keywords or [])
    monkeypatch.setattr(checker, "is_whitelisted", lambda gid, uid: uid in whitelisted_ids)
    monkeypatch.setattr(checker, "is_false_positive", lambda gid, uid: False)
    monkeypatch.setattr(checker, "get_known_bad_actor", lambda uid: bad_actor)


def _snap(**kw):
    base = dict(user_id=999, username=None, first_name="X", last_name=None)
    base.update(kw)
    return UserSnapshot(**base)


# ── check_user ────────────────────────────────────────────────────────────────

def test_whitelisted_user_never_flagged(monkeypatch):
    _patch_db(monkeypatch, whitelisted_ids={999})
    res = asyncio.run(check_user(_snap(user_id=999), 1))
    assert res.flagged is False


def test_blocklist_hit_flags_full_confidence(monkeypatch):
    _patch_db(monkeypatch, group={"use_global_blocklist": True},
              bad_actor={"reason": "manual ban"})
    res = asyncio.run(check_user(_snap(user_id=555), 1))
    assert res.flagged is True
    assert res.match_type == "known_bad_actor"
    assert res.score == 100.0


def test_blocklist_skipped_when_group_opted_out(monkeypatch):
    _patch_db(monkeypatch, group={"use_global_blocklist": False},
              bad_actor={"reason": "manual ban"})
    res = asyncio.run(check_user(_snap(user_id=555), 1))
    assert res.flagged is False


def test_username_impersonation_flagged(monkeypatch):
    wl = [{"user_id": 1, "username": "realadmin", "first_name": "Real",
           "last_name": "Admin", "pfp_hash": None}]
    _patch_db(monkeypatch, group=None, whitelist=wl)
    res = asyncio.run(check_user(_snap(user_id=2, username="realadmin", first_name="R"), 1))
    assert res.flagged is True
    assert res.match_type == "username"


def test_per_type_username_threshold_respected(monkeypatch):
    # A close-but-not-exact username; with a very high username_threshold it
    # should NOT flag, proving the per-type threshold is what's applied.
    wl = [{"user_id": 1, "username": "cryptoboss", "first_name": "C",
           "last_name": "", "pfp_hash": None}]
    _patch_db(monkeypatch, group={"username_threshold": 99}, whitelist=wl)
    res = asyncio.run(check_user(_snap(user_id=2, username="cryptobozz", first_name="C"), 1))
    assert res.flagged is False


def test_keyword_match_flagged(monkeypatch):
    _patch_db(monkeypatch, group=None, keywords=[{"pattern": "support", "is_regex": False}])
    res = asyncio.run(check_user(_snap(user_id=2, first_name="Official Support"), 1))
    assert res.flagged is True
    assert res.match_type == "keyword"


# ── ban_and_log score bands ───────────────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.banned = []
        self.unbanned = []

    async def ban(self, gid, uid):
        self.banned.append((gid, uid))

    async def unban(self, gid, uid):
        self.unbanned.append((gid, uid))


def _run_ban_and_log(monkeypatch, *, score, match_type, group):
    monkeypatch.setattr(checker, "get_group", lambda gid: group)
    logged = {}
    monkeypatch.setattr(checker, "insert_log", lambda **kw: logged.update(kw))
    rec = _Recorder()
    result = checker.DetectionResult(
        flagged=True, match_type=match_type, matched_val="x", score=score,
        target_user_id=None, target_name="Target",
    )
    asyncio.run(ban_and_log(
        result=result, snapshot=_snap(user_id=2), group_id=1,
        trigger="test", ban_func=rec.ban, unban_func=rec.unban,
    ))
    return rec, logged


def test_high_score_executes_ban(monkeypatch):
    rec, logged = _run_ban_and_log(
        monkeypatch, score=95, match_type="name",
        group={"action_mode": "ban", "ban_score": 90, "alert_score": 78},
    )
    assert rec.banned == [(1, 2)]
    assert logged.get("action_taken") == "banned"


def test_mid_score_downgrades_to_alert(monkeypatch):
    rec, logged = _run_ban_and_log(
        monkeypatch, score=82, match_type="name",
        group={"action_mode": "ban", "ban_score": 90, "alert_score": 78},
    )
    assert rec.banned == []                       # not banned
    assert logged.get("action_taken") == "alerted"


def test_low_score_ignored_no_log(monkeypatch):
    rec, logged = _run_ban_and_log(
        monkeypatch, score=70, match_type="name",
        group={"action_mode": "ban", "ban_score": 90, "alert_score": 78},
    )
    assert rec.banned == []
    assert logged == {}                           # returned before insert_log


def test_keyword_match_always_ban_band(monkeypatch):
    # keyword score in the result is 100-ish but match_type triggers the
    # full-confidence path regardless → ban executed.
    rec, logged = _run_ban_and_log(
        monkeypatch, score=100, match_type="keyword",
        group={"action_mode": "ban", "ban_score": 90, "alert_score": 78},
    )
    assert rec.banned == [(1, 2)]
    assert logged.get("action_taken") == "banned"
