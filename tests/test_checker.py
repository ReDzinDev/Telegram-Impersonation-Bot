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
    """
    The whitelist must contain an entry this user would otherwise match on,
    otherwise check_user short-circuits at `if not whitelist` and this test
    passes even with the immunity check deleted.
    """
    admin = {"user_id": 42, "username": "adminboss", "first_name": "Admin",
             "last_name": "Boss", "pfp_hash": None}
    _patch_db(monkeypatch, whitelist=[admin], whitelisted_ids={999})

    # Sanity: an identical non-whitelisted user IS flagged, so the whitelist
    # entry is genuinely matchable and the assertion below means something.
    unprotected = asyncio.run(
        check_user(_snap(user_id=1000, first_name="Admin", last_name="Boss"), 1)
    )
    assert unprotected.flagged is True

    res = asyncio.run(
        check_user(_snap(user_id=999, first_name="Admin", last_name="Boss"), 1)
    )
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
    """
    'cryptoboss1' vs 'cryptoboss' scores 95 — deliberately BETWEEN the global
    default (88) and the group override (99), so honouring the override and
    ignoring it give opposite answers. A pair scoring below both (the previous
    fixture scored 80) passes either way.
    """
    admin = {"user_id": 42, "username": "cryptoboss", "first_name": "Crypto",
             "last_name": "Boss", "pfp_hash": None}

    # Override of 99: 95 is not enough, so no username flag.
    _patch_db(monkeypatch, whitelist=[admin],
              group={"username_threshold": 99, "name_threshold": 99})
    strict = asyncio.run(check_user(
        _snap(user_id=7, username="cryptoboss1", first_name="Zed"), 1))
    assert strict.match_type != "username"

    # Same pair, permissive override: now it flags.
    _patch_db(monkeypatch, whitelist=[admin],
              group={"username_threshold": 90, "name_threshold": 99})
    loose = asyncio.run(check_user(
        _snap(user_id=7, username="cryptoboss1", first_name="Zed"), 1))
    assert loose.flagged is True
    assert loose.match_type == "username"

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
    # score=0 is BELOW alert_score, so this only bans if the match_type
    # full-confidence override actually fires. Passing score=100 here would
    # reach the ban band through the ordinary similarity path instead, and the
    # override could be deleted without failing.
    rec, logged = _run_ban_and_log(
        monkeypatch, score=0, match_type="keyword",
        group={"action_mode": "ban", "ban_score": 90, "alert_score": 78},
    )
    assert rec.banned == [(1, 2)]
    assert logged.get("action_taken") == "banned"


# ── unknown configuration must not default to the destructive action (F-3) ────

def test_unknown_group_config_alerts_instead_of_banning(monkeypatch):
    """
    get_group() returns None for an unregistered group AND for a failed read.
    Defaulting that to "ban" means a group explicitly set to alert-only starts
    banning during any database blip. Unknown config must be the safe action.
    """
    rec, logged = _run_ban_and_log(
        monkeypatch, score=100, match_type="keyword", group=None,
    )
    assert rec.banned == []
    assert logged.get("action_taken") == "alerted"


def test_alert_mode_never_bans_even_at_full_confidence(monkeypatch):
    """Mutation-survivor guard: deleting the action_mode=='alert' branch must fail."""
    rec, logged = _run_ban_and_log(
        monkeypatch, score=100, match_type="keyword",
        group={"action_mode": "alert", "ban_score": 90, "alert_score": 78},
    )
    assert rec.banned == []
    assert logged.get("action_taken") == "alerted"


# ── mutation-survivor guards ──────────────────────────────────────────────────
# Each of these covers an invariant that a mutation audit showed the suite could
# not detect: the code was correct, but deleting the safeguard kept every test
# green. They exist to make the safeguard's removal fail loudly.

def test_sentinel_accounts_are_skipped_before_any_scoring(monkeypatch):
    """GroupAnonymousBot / Channel Bot post as the group itself and must never be flagged."""
    admin = {"user_id": 42, "username": "grp", "first_name": "Group",
             "last_name": "Anonymous Bot", "pfp_hash": None}
    _patch_db(monkeypatch, whitelist=[admin])
    for sentinel_id in checker._SKIP_USER_IDS:
        res = asyncio.run(check_user(
            _snap(user_id=sentinel_id, first_name="Group", last_name="Anonymous Bot"), 1))
        assert res.flagged is False, f"sentinel {sentinel_id} was flagged"


def test_user_is_never_matched_against_their_own_whitelist_row(monkeypatch):
    """
    A whitelisted user whose immunity check is bypassed must still not match
    themselves — the `others` self-exclusion is the second line of defence.
    """
    me = {"user_id": 999, "username": "adminboss", "first_name": "Admin",
          "last_name": "Boss", "pfp_hash": None}
    # whitelisted_ids deliberately empty: immunity off, self-exclusion under test.
    _patch_db(monkeypatch, whitelist=[me], whitelisted_ids=set())
    res = asyncio.run(check_user(
        _snap(user_id=999, username="adminboss", first_name="Admin", last_name="Boss"), 1))
    assert res.flagged is False


def test_false_positive_grace_window_suppresses_detection(monkeypatch):
    """A user cleared as a false positive keeps their 30-day grace window."""
    admin = {"user_id": 42, "username": "adminboss", "first_name": "Admin",
             "last_name": "Boss", "pfp_hash": None}
    _patch_db(monkeypatch, whitelist=[admin])
    monkeypatch.setattr(checker, "is_false_positive", lambda gid, uid: True)
    res = asyncio.run(check_user(
        _snap(user_id=1000, first_name="Admin", last_name="Boss"), 1))
    assert res.flagged is False


def test_kick_mode_unbans_so_the_user_can_rejoin(monkeypatch):
    """Without the follow-up unban, 'kick' is a silent permanent ban."""
    rec, logged = _run_ban_and_log(
        monkeypatch, score=95, match_type="name",
        group={"action_mode": "kick", "ban_score": 90, "alert_score": 78},
    )
    assert rec.banned == [(1, 2)]
    assert rec.unbanned == [(1, 2)]
    assert logged.get("action_taken") == "kicked"


def _structured_avatar_bytes() -> bytes:
    """A hashable avatar — a flat colour would be rejected as degenerate."""
    from io import BytesIO
    from PIL import Image
    img = Image.new("RGB", (64, 64), (30, 60, 120))
    for x in range(64):
        for y in range(64):
            if (x // 7 + y // 5) % 2 == 0:
                img.putpixel((x, y), (200, 60, 60))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_photo_match_requires_a_weak_name_match_first(monkeypatch):
    """
    The photo stage is a tiebreaker, not a standalone signal. A user whose name
    resembles nothing on the whitelist must not be flagged on a photo alone —
    even when the photo is a byte-identical match for the admin's stored hash.
    """
    from src.utils.image import compute_pfp_hash_bytes

    avatar = _structured_avatar_bytes()
    admin = {"user_id": 42, "username": "zoltanvex", "first_name": "Zoltan",
             "last_name": "Vex", "pfp_hash": compute_pfp_hash_bytes(avatar)}
    assert admin["pfp_hash"] is not None      # the photo signal really is present
    _patch_db(monkeypatch, whitelist=[admin])

    res = asyncio.run(check_user(
        _snap(user_id=1000, first_name="Completely", last_name="Different",
              pfp_bytes=avatar), 1))
    assert res.match_type != "pfp"
    assert res.flagged is False
