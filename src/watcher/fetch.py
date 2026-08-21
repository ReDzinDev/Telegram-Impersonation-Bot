"""
Shared Pyrogram (MTProto) fetch helpers.

Profile-photo and bio fetching were previously duplicated across sweep.py,
events.py, and commands.py — three slightly-different copies of the same
stream-media / GetFullUser logic. Centralizing them here removes the
duplication (and the member_join -> events import cycle that existed only
to reach _fetch_bio).

Rate limiting lives here too, so every caller is paced the same way.
Telegram enforces a sustained per-method budget on heavy calls like
users.GetFullUser; purely reactive handling (wait out the FloodWait, then
resume at full speed) trips the limit again the moment it expires — a
burst/flood/burst cycle that Telegram escalates against (PEER_FLOOD /
account limitation). Each call kind therefore gets an adaptive _Pacer:

  - proactive: a minimum interval between calls, so we stay under the
    budget instead of discovering it the hard way;
  - reactive: on FloodWait, honor the mandated wait PLUS escalating
    padding (5s, 10s, 20s… capped at 60s) and ratchet the interval up
    1.5x (capped), so repeated floods slow us down instead of repeating;
  - forgiving: pacing resets to the base interval after 10 flood-free
    minutes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from io import BytesIO
from typing import Optional

from pyrogram import Client, raw
from pyrogram.errors import FloodWait

from src.config import BIO_FETCH_MIN_INTERVAL, PFP_FETCH_MIN_INTERVAL
from src.utils.image import compute_pfp_hash_bytes

logger = logging.getLogger(__name__)

# How long a caller will stall waiting for a slot before skipping the fetch.
# Event-driven callers (member join, profile-change updates) skip quickly so
# handlers stay responsive; sweeps pass wait=True and ride out cooldowns —
# their time budget is SWEEP_HARD_CAP_SECONDS, not milliseconds.
_EVENT_MAX_WAIT = 10.0
_SWEEP_MAX_WAIT = 300.0

_INTERVAL_CAP  = 10.0   # pacing interval never ratchets beyond this
_FORGIVE_AFTER = 600.0  # flood-free seconds before pacing resets to base
_BASE_PADDING  = 5.0    # extra cooldown on top of Telegram's mandated wait
_PADDING_CAP   = 60.0


class _Pacer:
    """Adaptive rate limiter for one kind of MTProto call (see module doc)."""

    def __init__(self, name: str, base_interval: float):
        self.name = name
        self.base_interval = base_interval
        self.interval = base_interval
        self._lock = asyncio.Lock()
        self._next_slot = 0.0     # monotonic time the next call may fire
        self._flood_until = 0.0   # monotonic time the current cooldown ends
        self._flood_streak = 0    # floods since the last forgiveness reset
        self._last_flood = 0.0

    def cooldown_remaining(self) -> float:
        return max(0.0, self._flood_until - time.monotonic())

    def _pending_wait(self) -> float:
        now = time.monotonic()
        return max(self._flood_until - now, self._next_slot - now, 0.0)

    def _maybe_forgive(self) -> None:
        """
        Reset pacing after a genuinely quiet stretch.

        Quiet is measured from when the cooldown ENDS, not from when the flood
        was recorded. on_flood honours arbitrarily long mandated waits, so
        measuring from _last_flood meant any FloodWait longer than
        _FORGIVE_AFTER forgot everything while still cooling down — the sweep
        then resumed at full base speed with a virgin escalation ladder, in
        exactly the severe case the ratchet exists for.
        """
        if not self._last_flood:
            return
        quiet_since = max(self._last_flood, self._flood_until)
        if time.monotonic() - quiet_since > _FORGIVE_AFTER:
            self.interval = self.base_interval
            self._flood_streak = 0
            self._last_flood = 0.0

    async def acquire(self, max_wait: float) -> bool:
        """
        Wait for a call slot; True means "go ahead". Returns False — the caller
        should skip the fetch — when the slot lies beyond max_wait.

        The slot is RESERVED under the lock and waited for outside it. Sleeping
        while holding the lock made max_wait a lie: _pending_wait() only ever
        saw _next_slot, never how many callers were already queued, so with N
        callers the Nth slept (N-1) * interval while the ceiling check believed
        the wait was one interval. Measured at 6 concurrent callers with a 2s
        ceiling, waits reached 5s. It also serialised every caller behind one
        sleeper — which on Pyrogram's handler workers meant the update queue
        backed up and the watcher went deaf.

        After waking we re-check the cooldown, because the RPC itself happens
        outside the lock: another caller's in-flight call can record a flood
        while we wait, and firing into it earns a fresh FloodWait and another
        rung on the escalation ladder.
        """
        self._maybe_forgive()
        deadline = time.monotonic() + max_wait

        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot, self._flood_until)
            if slot > deadline:
                return False
            # Reserve before releasing, so queued callers compute their own slot
            # from an already-advanced _next_slot instead of all picking this one.
            self._next_slot = slot + self.interval

        while True:
            now = time.monotonic()
            target = max(slot, self._flood_until)
            if target <= now:
                return True
            if target > deadline:
                # A cooldown landed while we waited and pushed us past the
                # ceiling. The reserved slot is left consumed, which paces the
                # next caller slightly more conservatively — the safe direction.
                return False
            await asyncio.sleep(target - now)

    def on_flood(self, mandated_seconds: float) -> float:
        """Record a FloodWait; returns the total cooldown being applied."""
        self._flood_streak += 1
        self._last_flood = time.monotonic()
        padding = min(_BASE_PADDING * (2 ** (self._flood_streak - 1)), _PADDING_CAP)
        total = mandated_seconds + padding
        self._flood_until = max(self._flood_until, time.monotonic() + total)
        self.interval = min(self.interval * 1.5, _INTERVAL_CAP)
        return total


# Bio fetches (users.GetFullUser) and PFP fetches (photo list + media
# download) hit different server-side budgets, so they pace independently.
_bio_pacer = _Pacer("bio", BIO_FETCH_MIN_INTERVAL)
_pfp_pacer = _Pacer("pfp", PFP_FETCH_MIN_INTERVAL)


def report_flood(seconds: float, *, kind: str = "all") -> None:
    """
    Record a FloodWait observed OUTSIDE the paced fetch helpers.

    Rate limits are per-account, not per-call-site, so a flood learned by the
    sweep's member enumeration or the health probe is information the bio and
    photo pacers need too. Without this they kept issuing calls into a DC that
    had just pushed back, and each one earned its own FloodWait — ratcheting the
    escalation ladder from what was really a single event.

    kind selects a specific pacer ("bio" / "pfp"); the default backs off both,
    which is right for a whole-account signal like PEER_FLOOD.
    """
    targets = {"bio": (_bio_pacer,), "pfp": (_pfp_pacer,)}.get(
        kind, (_bio_pacer, _pfp_pacer)
    )
    for pacer in targets:
        total = pacer.on_flood(seconds)
        logger.warning(
            f"[{pacer.name}] external flood reported ({seconds:.0f}s mandated) — "
            f"cooling down {total:.0f}s, interval now {pacer.interval:.1f}s."
        )


def bio_cooldown_remaining() -> float:
    """Seconds until GetFullUser calls may resume (0 when not cooling down)."""
    return _bio_pacer.cooldown_remaining()


def pfp_cooldown_remaining() -> float:
    """
    Seconds until profile-photo downloads may resume (0 when not cooling down).

    The counterpart to bio_cooldown_remaining, which existed while this did not
    — so the sweep could not tell a photo fetch the pacer SKIPPED from a user who
    simply has no avatar. It counted the second case, so a weak name match that
    specifically needed photo confirmation was resolved as clean whenever the
    pacer was cooling down.
    """
    return _pfp_pacer.cooldown_remaining()


async def fetch_pfp_bytes(pyro: Client, user_id: int, *, wait: bool = False) -> Optional[bytes]:
    """
    Download a user's current profile photo as raw bytes, or None.

    wait=True (sweeps) rides out flood cooldowns up to a few minutes; the
    default skips instead so event handlers stay responsive.
    """
    if not await _pfp_pacer.acquire(_SWEEP_MAX_WAIT if wait else _EVENT_MAX_WAIT):
        return None
    try:
        photos = pyro.get_chat_photos(user_id, limit=1)
        photo = await photos.__anext__()
        buf = BytesIO()
        async for chunk in pyro.stream_media(photo):
            buf.write(chunk)
        return buf.getvalue() or None
    except StopAsyncIteration:
        return None
    except FloodWait as e:
        total = _pfp_pacer.on_flood(e.value)
        logger.warning(
            f"PFP flood wait {e.value}s for user {user_id} — cooling down {total:.0f}s, "
            f"pacing now {_pfp_pacer.interval:.1f}s/call."
        )
        return None
    except Exception as e:
        logger.debug(f"PFP fetch failed for user {user_id}: {e}")
        return None


async def fetch_pfp_hash(pyro: Client, user_id: int, *, wait: bool = False) -> Optional[str]:
    """Download + perceptual-hash a user's profile photo, or None."""
    data = await fetch_pfp_bytes(pyro, user_id, wait=wait)
    return compute_pfp_hash_bytes(data) if data else None


async def fetch_bio(pyro: Client, user_id: int, *, wait: bool = False) -> Optional[str]:
    """
    Fetch a user's bio / about text via MTProto GetFullUser, or None.

    wait=True (sweeps) rides out flood cooldowns up to a few minutes; the
    default skips instead so event handlers stay responsive.
    """
    if not await _bio_pacer.acquire(_SWEEP_MAX_WAIT if wait else _EVENT_MAX_WAIT):
        return None
    try:
        peer = await pyro.resolve_peer(user_id)
        full = await pyro.invoke(raw.functions.users.GetFullUser(id=peer))
        return full.full_user.about or None
    except FloodWait as e:
        total = _bio_pacer.on_flood(e.value)
        logger.warning(
            f"Bio flood wait {e.value}s for user {user_id} — cooling down {total:.0f}s, "
            f"pacing now {_bio_pacer.interval:.1f}s/call."
        )
        return None
    except Exception as e:
        logger.debug(f"Bio fetch failed for user {user_id}: {e}")
        return None
