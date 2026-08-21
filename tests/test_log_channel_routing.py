"""
Detections must route to the group's own log channel (B-2).

sweep.py and events.py passed the *global* LOG_CHANNEL_ID straight to
make_action_funcs, while every other detection path resolved the per-group
override first (messages.py, member_join.py, commands._resolve_log_channel).
The sweep even resolved it correctly for its own summary, but not for the
detections themselves.

make_action_funcs returns log_notify=None when the channel is falsy, so on a
deployment where each group sets its own /setlogchannel and no global channel
exists, users were banned with NO alert and NO undo button — the admin saw only
"Flagged: 3" in the sweep summary and could never learn who was banned.
"""
import asyncio


from src.utils import checker
from src.utils.checker import DetectionResult, UserSnapshot, resolve_log_channel
from src.watcher import events, sweep


GLOBAL_CHANNEL = "-1009999999999"
GROUP_CHANNEL = -1001111111111
GID = -1002222222222


# ── the resolver itself ───────────────────────────────────────────────────────

def test_group_channel_wins_over_global(monkeypatch):
    monkeypatch.setattr(checker, "get_group",
                        lambda gid: {"log_channel_id": GROUP_CHANNEL})
    assert resolve_log_channel(GID, GLOBAL_CHANNEL) == GROUP_CHANNEL


def test_global_is_used_when_group_has_no_override(monkeypatch):
    monkeypatch.setattr(checker, "get_group", lambda gid: {"log_channel_id": None})
    assert resolve_log_channel(GID, GLOBAL_CHANNEL) == GLOBAL_CHANNEL


def test_group_channel_is_used_when_there_is_no_global(monkeypatch):
    """The reported silent-ban configuration: per-group channels, no global one."""
    monkeypatch.setattr(checker, "get_group",
                        lambda gid: {"log_channel_id": GROUP_CHANNEL})
    assert resolve_log_channel(GID, None) == GROUP_CHANNEL


def test_unavailable_group_config_falls_back_to_global(monkeypatch):
    from src.db import DatabaseUnavailable

    def boom(gid):
        raise DatabaseUnavailable("down")
    monkeypatch.setattr(checker, "get_group", boom)
    assert resolve_log_channel(GID, GLOBAL_CHANNEL) == GLOBAL_CHANNEL


# ── the two paths that bypassed it ────────────────────────────────────────────

def _capture_channel(monkeypatch):
    """Record the channel make_action_funcs is built with."""
    seen = {}

    def fake_make_action_funcs(bot, log_channel_id):
        seen["channel"] = log_channel_id
        async def _noop(*a, **k):
            pass
        return _noop, _noop, None

    monkeypatch.setattr(checker, "make_action_funcs", fake_make_action_funcs)

    async def fake_ban_and_log(**kw):
        seen["notified"] = kw.get("log_channel_notify")
    monkeypatch.setattr(events, "ban_and_log", fake_ban_and_log, raising=False)
    monkeypatch.setattr(sweep, "ban_and_log", fake_ban_and_log, raising=False)
    return seen


def test_profile_change_detection_uses_the_group_channel(monkeypatch):
    seen = _capture_channel(monkeypatch)
    monkeypatch.setattr(checker, "get_group",
                        lambda gid: {"log_channel_id": GROUP_CHANNEL})

    async def flagged(snapshot, group_id):
        return DetectionResult(flagged=True, match_type="name",
                               matched_val="x", score=95)
    monkeypatch.setattr(events, "check_user", flagged)

    snap = UserSnapshot(user_id=5, username=None, first_name="A", last_name=None)
    asyncio.run(events._check_and_act(
        pyro=None, bot=object(), snapshot=snap, group_ids=[GID],
        trigger="name_change", log_channel_id=None,
    ))
    assert seen["channel"] == GROUP_CHANNEL
