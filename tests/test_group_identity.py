"""
A missing profile photo must not skip the group-identity check (E-3).

Stage 4 is a photo tiebreaker for weak name matches. When it needed a photo and
the snapshot had none, it RETURNED `needs_pfp=True` — which aborted the pipeline
before stage 5, the group-identity check.

Only sweep.py honours that signal and re-runs. member_join.py, messages.py and
events.py all fetch the photo eagerly and just see `flagged=False`, so for them
the abort fired whenever the user simply had no profile photo (or the download
failed). Deleting your avatar was enough to skip the check for impersonating the
group itself.

The reported case: a group titled "Binance Official Announcements" with a
single-token protected admin named "Binance". A user takes the group's exact
name, removes their photo, and is not flagged at all.
"""
import asyncio


from src.utils import checker
from src.utils.checker import UserSnapshot, check_user


GROUP_TITLE = "Binance Official Announcements"
GROUP_LOGO = "80ff00ff00ff00fb"


def _patch(monkeypatch, *, whitelist, group):
    monkeypatch.setattr(checker, "is_whitelisted", lambda g, u: False)
    monkeypatch.setattr(checker, "is_false_positive", lambda g, u: False)
    monkeypatch.setattr(checker, "get_known_bad_actor", lambda u: None)
    monkeypatch.setattr(checker, "get_reserved_keywords", lambda g: [])
    monkeypatch.setattr(checker, "get_whitelist", lambda g: whitelist)
    monkeypatch.setattr(checker, "get_group", lambda g: group)


def _admin(first="Binance", last=None, pfp="1122334455667788"):
    return {"user_id": 42, "username": "binance", "first_name": first,
            "last_name": last, "pfp_hash": pfp}


def _snap(name, *, pfp_bytes=None, uid=1000):
    first, _, last = name.partition(" ")
    return UserSnapshot(user_id=uid, username=None, first_name=first,
                        last_name=last or None, pfp_bytes=pfp_bytes)


def test_group_impersonator_without_a_photo_is_still_caught(monkeypatch):
    """
    The reported scenario. The weak match against single-token admin "Binance"
    activated the photo tiebreaker, which bailed out because there was no photo
    — and took the group-identity check down with it.
    """
    _patch(monkeypatch,
           whitelist=[_admin()],
           group={"title": GROUP_TITLE, "pfp_hash": GROUP_LOGO})

    res = asyncio.run(check_user(_snap(GROUP_TITLE), -100))
    assert res.flagged is True, "removing your avatar skipped the group check"
    assert res.match_type == "group_name"
    assert res.score == 100


def test_the_photo_tiebreaker_still_asks_for_a_photo(monkeypatch):
    """
    needs_pfp must survive as a signal — sweep.py relies on it to lazily fetch a
    photo and re-run, which is what keeps the sweep cheap.
    """
    _patch(monkeypatch,
           whitelist=[_admin()],
           group={"title": "Some Unrelated Group", "pfp_hash": None})

    res = asyncio.run(check_user(_snap("Binance"), -100))
    assert res.flagged is False
    assert res.needs_pfp is True, "the lazy-load signal was lost"


def test_needs_pfp_is_still_reported_after_the_group_stage_runs(monkeypatch):
    """
    The flag now rides on the final result rather than short-circuiting, so a
    caller that can fetch a photo still learns it should.
    """
    _patch(monkeypatch,
           whitelist=[_admin()],
           group={"title": "Totally Different Name", "pfp_hash": GROUP_LOGO})

    res = asyncio.run(check_user(_snap("Binance"), -100))
    assert res.flagged is False
    assert res.needs_pfp is True


def test_group_logo_tiebreaker_also_requests_a_photo(monkeypatch):
    """Stage 5's own weak-match path has the same lazy-load contract."""
    _patch(monkeypatch,
           whitelist=[],
           group={"title": "Binance", "pfp_hash": GROUP_LOGO})

    res = asyncio.run(check_user(_snap("Binance"), -100))
    assert res.flagged is False
    assert res.needs_pfp is True


def test_a_strong_admin_name_match_still_wins_before_the_group_stage(monkeypatch):
    """Ordering must not change: an admin impersonation is still reported as such."""
    _patch(monkeypatch,
           whitelist=[_admin(first="Vitalik", last="Buterin")],
           group={"title": GROUP_TITLE, "pfp_hash": GROUP_LOGO})

    res = asyncio.run(check_user(_snap("Vitalik Buterin"), -100))
    assert res.flagged is True
    assert res.match_type == "name"


def test_unregistered_group_cannot_match_group_identity(monkeypatch):
    """No group config means no title to compare against — and no crash."""
    _patch(monkeypatch, whitelist=[_admin()], group=None)
    res = asyncio.run(check_user(_snap(GROUP_TITLE), -100))
    assert res.match_type != "group_name"
