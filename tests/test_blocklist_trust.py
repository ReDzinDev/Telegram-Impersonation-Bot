"""
Cross-group blocklist trust boundary (A-1, A-2).

known_bad_actors is global and consulted for every non-whitelisted user at score
100 — always the ban band — but the only authorisation to write it was "admin of
the group named in the payload", and member_join auto-registers any group the bot
is added to. So anyone could add the bot to their own group, grant it ban rights,
and /ban an arbitrary user id: the entry landed in the shared table and the next
sweep banned that user out of every other group. A group that had opted OUT of
the blocklist still contributed to it.

The rule now: a per-group admin's action may not carry global ban authority.
Entries whose source group isn't trusted are advisory — they still surface as an
alert for a human, they just cannot execute a ban on their own.
"""
import asyncio

import pytest

from src.utils import checker
from src.utils.checker import DetectionResult, UserSnapshot, check_user


THIS_GROUP = -1001111111111
TRUSTED_GROUP = -1002222222222
STRANGER_GROUP = -1003333333333


def _patch(monkeypatch, *, bad_actor, trusted=frozenset()):
    monkeypatch.setattr(checker, "is_whitelisted", lambda gid, uid: False)
    monkeypatch.setattr(checker, "is_false_positive", lambda gid, uid: False)
    monkeypatch.setattr(checker, "get_group", lambda gid: {"use_global_blocklist": True})
    monkeypatch.setattr(checker, "get_known_bad_actor", lambda uid: bad_actor)
    monkeypatch.setattr(checker, "get_whitelist", lambda gid: [])
    monkeypatch.setattr(checker, "get_reserved_keywords", lambda gid: [])
    monkeypatch.setattr(checker, "BLOCKLIST_TRUSTED_GROUPS", frozenset(trusted))


def _snap(uid=555):
    return UserSnapshot(user_id=uid, username="x", first_name="X", last_name=None)


def test_entry_from_an_untrusted_group_is_advisory_only(monkeypatch):
    _patch(monkeypatch, bad_actor={"reason": "manual ban",
                                   "source_group_id": STRANGER_GROUP})
    res = asyncio.run(check_user(_snap(), THIS_GROUP))
    assert res.flagged is True
    assert res.match_type == "known_bad_actor"
    assert res.advisory is True, "a stranger's group must not carry ban authority"


def test_entry_from_a_trusted_group_is_actionable(monkeypatch):
    _patch(monkeypatch,
           bad_actor={"reason": "manual ban", "source_group_id": TRUSTED_GROUP},
           trusted={TRUSTED_GROUP})
    res = asyncio.run(check_user(_snap(), THIS_GROUP))
    assert res.flagged is True
    assert res.advisory is False


def test_entry_this_group_created_itself_is_actionable(monkeypatch):
    """A group's own ban is authoritative within that same group."""
    _patch(monkeypatch, bad_actor={"reason": "manual ban",
                                   "source_group_id": THIS_GROUP})
    res = asyncio.run(check_user(_snap(), THIS_GROUP))
    assert res.flagged is True
    assert res.advisory is False


def test_entry_with_unknown_provenance_is_advisory(monkeypatch):
    """Legacy rows predating source_group_id must not be trusted by default."""
    _patch(monkeypatch, bad_actor={"reason": "manual ban", "source_group_id": None})
    res = asyncio.run(check_user(_snap(), THIS_GROUP))
    assert res.advisory is True


# ── advisory results must not execute a ban ───────────────────────────────────

class _Recorder:
    def __init__(self):
        self.banned = []
        self.unbanned = []

    async def ban(self, gid, uid):
        self.banned.append((gid, uid))

    async def unban(self, gid, uid):
        self.unbanned.append((gid, uid))


def test_advisory_detection_alerts_even_in_ban_mode(monkeypatch):
    monkeypatch.setattr(checker, "get_group",
                        lambda gid: {"action_mode": "ban", "ban_score": 90,
                                     "alert_score": 78})
    logged = {}
    monkeypatch.setattr(checker, "insert_log", lambda **kw: logged.update(kw))
    rec = _Recorder()
    asyncio.run(checker.ban_and_log(
        result=DetectionResult(flagged=True, match_type="known_bad_actor",
                               matched_val="manual ban", score=100.0,
                               advisory=True),
        snapshot=_snap(), group_id=THIS_GROUP, trigger="test",
        ban_func=rec.ban, unban_func=rec.unban,
    ))
    assert rec.banned == []
    assert logged.get("action_taken") == "alerted"


def test_non_advisory_blocklist_hit_still_bans(monkeypatch):
    """The feature must keep working for trusted sources."""
    monkeypatch.setattr(checker, "get_group",
                        lambda gid: {"action_mode": "ban", "ban_score": 90,
                                     "alert_score": 78})
    logged = {}
    monkeypatch.setattr(checker, "insert_log", lambda **kw: logged.update(kw))
    rec = _Recorder()
    asyncio.run(checker.ban_and_log(
        result=DetectionResult(flagged=True, match_type="known_bad_actor",
                               matched_val="manual ban", score=100.0),
        snapshot=_snap(), group_id=THIS_GROUP, trigger="test",
        ban_func=rec.ban, unban_func=rec.unban,
    ))
    assert rec.banned == [(THIS_GROUP, 555)]
    assert logged.get("action_taken") == "banned"


# ── config ────────────────────────────────────────────────────────────────────

def test_trusted_groups_parse_from_env(monkeypatch):
    from src.config import _parse_group_ids

    assert _parse_group_ids("-100123, -100456") == frozenset({-100123, -100456})
    assert _parse_group_ids("") == frozenset()
    assert _parse_group_ids(None) == frozenset()
    assert _parse_group_ids("-100123,junk,-100456") == frozenset({-100123, -100456})


# ── A-2: clearing a global entry needs authority over it ──────────────────────
#
# remove_known_bad_actor(user_id) was global and reachable from the alert buttons
# with only per-group admin rights, so a user blocklisted everywhere could enroll
# their own group, press Unban there, and regain automatic entry to every other
# group. The per-group reversal (whitelist / false-positive record) is the right
# scope for a group admin; clearing the SHARED entry is not theirs to do.

from src import db as db_mod


class _FakeCursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount

    def execute(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False


class _FakeConn:
    def __init__(self, rowcount=1):
        self._rc = rowcount

    def cursor(self):
        return _FakeCursor(self._rc)

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def blocklist_db(monkeypatch):
    def install(entry, rowcount=1, trusted=frozenset()):
        monkeypatch.setattr(db_mod, "get_connection", lambda *a, **k: _FakeConn(rowcount))
        monkeypatch.setattr(db_mod, "put_connection", lambda c: None)
        monkeypatch.setattr(db_mod, "get_known_bad_actor", lambda uid: entry)
        monkeypatch.setattr(db_mod, "BLOCKLIST_TRUSTED_GROUPS", frozenset(trusted))
        db_mod._bad_actor_cache.clear()
    return install


AUTH_CASES = [
    ({"source_group_id": THIS_GROUP}, THIS_GROUP, frozenset(), True,
     "a group's own entry"),
    ({"source_group_id": TRUSTED_GROUP}, THIS_GROUP, {TRUSTED_GROUP}, True,
     "an operator-trusted source"),
    ({"source_group_id": STRANGER_GROUP}, THIS_GROUP, frozenset(), False,
     "a stranger's group"),
    ({"source_group_id": None}, THIS_GROUP, frozenset(), False,
     "unknown provenance"),
    (None, THIS_GROUP, frozenset(), False, "no entry at all"),
]


@pytest.mark.parametrize("entry,gid,trusted,expected,label", AUTH_CASES)
def test_authority_truth_table(monkeypatch, entry, gid, trusted, expected, label):
    monkeypatch.setattr(db_mod, "BLOCKLIST_TRUSTED_GROUPS", frozenset(trusted))
    assert db_mod.blocklist_entry_is_authoritative(entry, gid) is expected, label


def test_removal_refused_for_another_groups_entry(blocklist_db):
    blocklist_db({"source_group_id": STRANGER_GROUP})
    assert db_mod.remove_known_bad_actor(555, acting_group_id=THIS_GROUP) is False


def test_removal_allowed_for_own_entry(blocklist_db):
    blocklist_db({"source_group_id": THIS_GROUP})
    assert db_mod.remove_known_bad_actor(555, acting_group_id=THIS_GROUP) is True


def test_removal_allowed_for_trusted_source(blocklist_db):
    blocklist_db({"source_group_id": TRUSTED_GROUP}, trusted={TRUSTED_GROUP})
    assert db_mod.remove_known_bad_actor(555, acting_group_id=THIS_GROUP) is True


def test_acting_group_is_required(blocklist_db):
    """An unscoped global delete must not be expressible."""
    blocklist_db({"source_group_id": THIS_GROUP})
    with pytest.raises(TypeError):
        db_mod.remove_known_bad_actor(555)


# ── A-1 write side: only trusted groups contribute ────────────────────────────
#
# Containing the damage on read isn't enough on its own: an untrusted group could
# still push arbitrary user ids into the shared table and generate an advisory
# alert in every other group for anyone it liked. Contribution is operator-scoped
# too. An untrusted group's /ban still bans locally via Telegram — it just
# doesn't propagate.

def test_untrusted_group_cannot_contribute_to_the_blocklist(blocklist_db):
    blocklist_db(None, trusted=frozenset())
    assert db_mod.add_known_bad_actor(
        user_id=555, username="x", full_name="X", reason="manual ban",
        confirmed_by=1, source_group_id=STRANGER_GROUP,
    ) is False


def test_trusted_group_can_contribute(blocklist_db):
    blocklist_db(None, trusted={TRUSTED_GROUP})
    assert db_mod.add_known_bad_actor(
        user_id=555, username="x", full_name="X", reason="manual ban",
        confirmed_by=1, source_group_id=TRUSTED_GROUP,
    ) is True
