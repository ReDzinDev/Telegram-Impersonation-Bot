"""
similarity_score must mean one thing (F-5).

For `pfp` and `group_pfp` matches, DetectionResult.score carried the raw Hamming
DISTANCE — 0-64, where LOWER is better. For every other match type it carried a
0-100 similarity where HIGHER is better. Both were written to the same
logs.similarity_score column and rendered through the same alert template.

So the strongest possible photo match displayed as `Score: 0` to the admin
deciding whether to keep the ban, and any query over that column mixed two
incompatible scales. ban_and_log papered over it for the ACTION decision by
forcing photo matches to full confidence, which is exactly why the display bug
went unnoticed: the bans were right, only the number was wrong.
"""
import asyncio

import pytest

from src.utils import checker
from src.utils.checker import DetectionResult, UserSnapshot, pfp_confidence


# ── the conversion ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("distance,expected", [
    (0, 100),    # identical
    (16, 75),
    (32, 50),
    (64, 0),     # maximally different
])
def test_distance_converts_to_a_confidence(distance, expected):
    assert pfp_confidence(distance) == expected


def test_confidence_is_monotonically_decreasing():
    scores = [pfp_confidence(d) for d in range(0, 65)]
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= s <= 100 for s in scores)


def test_a_threshold_edge_match_is_high_but_not_perfect():
    """At the default threshold of 10 the evidence is real but not identical."""
    assert 80 <= pfp_confidence(10) < 90


def test_out_of_range_distances_are_clamped():
    assert pfp_confidence(-5) == 100
    assert pfp_confidence(500) == 0


# ── what the detector reports ─────────────────────────────────────────────────

def _structured_avatar(seed=(200, 60, 60)) -> bytes:
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (64, 64), (30, 60, 120))
    for x in range(64):
        for y in range(64):
            if (x // 7 + y // 5) % 2 == 0:
                img.putpixel((x, y), seed)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _patch(monkeypatch, whitelist, group=None):
    monkeypatch.setattr(checker, "is_whitelisted", lambda g, u: False)
    monkeypatch.setattr(checker, "is_false_positive", lambda g, u: False)
    monkeypatch.setattr(checker, "get_known_bad_actor", lambda u: None)
    monkeypatch.setattr(checker, "get_reserved_keywords", lambda g: [])
    monkeypatch.setattr(checker, "get_whitelist", lambda g: whitelist)
    monkeypatch.setattr(checker, "get_group", lambda g: group)


def test_a_perfect_photo_match_scores_full_confidence(monkeypatch):
    """This is the headline symptom: it used to report 0."""
    from src.utils.image import compute_pfp_hash_bytes

    avatar = _structured_avatar()
    admin = {"user_id": 42, "username": "boss", "first_name": "Zoltan",
             "last_name": None, "pfp_hash": compute_pfp_hash_bytes(avatar)}
    _patch(monkeypatch, [admin])

    # Single-token admin name => weak name match => the photo tiebreaker runs.
    snap = UserSnapshot(user_id=1000, username=None, first_name="Zoltan",
                        last_name=None, pfp_bytes=avatar)
    res = asyncio.run(checker.check_user(snap, -100))
    assert res.match_type == "pfp"
    assert res.score == 100, f"a byte-identical photo scored {res.score}"
    assert res.pfp_distance == 0, "the raw distance should still be available"


def test_the_raw_distance_is_preserved_separately(monkeypatch):
    from src.utils.image import compute_pfp_hash_bytes

    avatar = _structured_avatar()
    admin = {"user_id": 42, "username": "boss", "first_name": "Zoltan",
             "last_name": None, "pfp_hash": compute_pfp_hash_bytes(avatar)}
    _patch(monkeypatch, [admin])
    snap = UserSnapshot(user_id=1000, username=None, first_name="Zoltan",
                        last_name=None, pfp_bytes=avatar)
    res = asyncio.run(checker.check_user(snap, -100))
    assert isinstance(res.pfp_distance, int)


def test_non_photo_matches_carry_no_distance(monkeypatch):
    admin = {"user_id": 42, "username": "zoltanvex", "first_name": "Zoltan",
             "last_name": "Vex", "pfp_hash": None}
    _patch(monkeypatch, [admin])
    snap = UserSnapshot(user_id=1000, username=None,
                        first_name="Zoltan", last_name="Vex")
    res = asyncio.run(checker.check_user(snap, -100))
    assert res.match_type == "name"
    assert res.pfp_distance is None


# ── what gets logged and shown ────────────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.banned = []

    async def ban(self, gid, uid):
        self.banned.append((gid, uid))


def _run_ban_and_log(monkeypatch, result):
    monkeypatch.setattr(checker, "get_group",
                        lambda g: {"action_mode": "ban", "ban_score": 90,
                                   "alert_score": 78})
    logged = {}
    monkeypatch.setattr(checker, "insert_log", lambda **kw: logged.update(kw))
    sent = []

    async def notify(text, markup=None):
        sent.append(text)

    rec = _Recorder()
    asyncio.run(checker.ban_and_log(
        result=result,
        snapshot=UserSnapshot(user_id=2, username=None, first_name="X",
                              last_name=None),
        group_id=1, trigger="test", ban_func=rec.ban,
        log_channel_notify=notify,
    ))
    return rec, logged, sent


def test_the_logged_score_is_a_confidence_not_a_distance(monkeypatch):
    result = DetectionResult(flagged=True, match_type="pfp", matched_val="abc",
                             score=100.0, pfp_distance=0)
    _, logged, _ = _run_ban_and_log(monkeypatch, result)
    assert logged["similarity_score"] == 100.0


def test_the_alert_shows_the_distance_alongside_the_confidence(monkeypatch):
    """An admin reviewing a photo match wants to know how close it was."""
    result = DetectionResult(flagged=True, match_type="pfp", matched_val="abc",
                             score=84.0, pfp_distance=10)
    _, _, sent = _run_ban_and_log(monkeypatch, result)
    assert sent, "no alert was sent"
    body = sent[0]
    assert "84" in body
    assert "10" in body and "64" in body, "the raw distance is not shown"


def test_a_name_match_alert_does_not_mention_a_distance(monkeypatch):
    result = DetectionResult(flagged=True, match_type="name",
                             matched_val="Someone", score=95.0)
    _, _, sent = _run_ban_and_log(monkeypatch, result)
    assert "/64" not in sent[0]


def test_photo_matches_still_execute_the_configured_action(monkeypatch):
    """
    Deliberately unchanged: the action decision still treats a photo match as
    full confidence. This fixes the REPORTED number, not when the bot bans.
    """
    result = DetectionResult(flagged=True, match_type="pfp", matched_val="abc",
                             score=84.0, pfp_distance=10)
    rec, logged, _ = _run_ban_and_log(monkeypatch, result)
    assert rec.banned == [(1, 2)]
    assert logged["action_taken"] == "banned"


# ── historical rows, and the mechanism for fixing them once ───────────────────
#
# Existing logs rows still hold distances in similarity_score. Converting them
# needs a migration that runs exactly ONCE — and the codebase had no way to
# express that, which is why the is_bot backfill re-ran on every boot and
# permanently reclassified handles like @talbot as bots after each redeploy.

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class _MigrationCursor:
    """Records statements; pretends the named migration is absent unless told."""

    def __init__(self, already_applied=()):
        self.statements = []
        self._applied = set(already_applied)
        self._last_lookup = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))
        if "FROM schema_migrations" in sql:
            self._last_lookup = (params or (None,))[0]

    def fetchone(self):
        if self._last_lookup is None:
            return None
        return {"1": 1} if self._last_lookup in self._applied else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_pending_migration_runs_and_is_recorded():
    from src.db import _run_once

    cur = _MigrationCursor()
    ran = _run_once(cur, "demo", "UPDATE t SET x = 1")
    assert ran is True
    sql = [s for s, _ in cur.statements]
    assert any("UPDATE t SET x = 1" in s for s in sql)
    assert any("INSERT INTO schema_migrations" in s for s in sql)


def test_an_applied_migration_is_skipped():
    from src.db import _run_once

    cur = _MigrationCursor(already_applied={"demo"})
    ran = _run_once(cur, "demo", "UPDATE t SET x = 1")
    assert ran is False
    assert not any("UPDATE t SET x = 1" in s for s, _ in cur.statements)


def test_multiple_statements_are_supported():
    from src.db import _run_once

    cur = _MigrationCursor()
    _run_once(cur, "demo", ["UPDATE a SET x = 1", "UPDATE b SET y = 2"])
    sql = [s for s, _ in cur.statements]
    assert any("UPDATE a" in s for s in sql)
    assert any("UPDATE b" in s for s in sql)


def test_the_migrations_table_is_created():
    ddl = (ROOT / "src" / "db.py").read_text(encoding="utf-8")
    assert re.search(r"CREATE TABLE IF NOT EXISTS schema_migrations", ddl)


def test_historical_photo_scores_are_converted_once():
    ddl = (ROOT / "src" / "db.py").read_text(encoding="utf-8")
    assert "_run_once(" in ddl
    # The conversion must target only photo rows, and use the same formula.
    assert re.search(r"detection_type IN \('pfp', ?'group_pfp'\)", ddl), (
        "the score conversion must apply only to photo matches"
    )


def test_the_is_bot_backfill_is_guarded():
    """
    It had no guard, so it re-ran on every boot: /import_admins would correct a
    human named @talbot, and the next redeploy flipped them back to a bot.
    """
    ddl = (ROOT / "src" / "db.py").read_text(encoding="utf-8")
    backfill = ddl[ddl.index("SET is_bot = TRUE") - 900:ddl.index("SET is_bot = TRUE")]
    assert "_run_once(" in backfill, "the is_bot backfill still runs on every boot"
