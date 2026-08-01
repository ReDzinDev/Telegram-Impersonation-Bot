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
        if self._last_flood and time.monotonic() - self._last_flood > _FORGIVE_AFTER:
            self.interval = self.base_interval
            self._flood_streak = 0
            self._last_flood = 0.0

    async def acquire(self, max_wait: float) -> bool:
        """
        Wait for a call slot; True means "go ahead". Returns False — caller
        should skip the fetch — when the pending wait exceeds max_wait.
        Concurrent callers queue on the lock, so a burst of events cannot
        stampede past the interval.
        """
        self._maybe_forgive()
        if self._pending_wait() > max_wait:
            return False
        async with self._lock:
            # Re-check under the lock: a FloodWait may have landed while queued.
            wait = self._pending_wait()
            if wait > max_wait:
                return False
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_slot = time.monotonic() + self.interval
            return True

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


def bio_cooldown_remaining() -> float:
    """Seconds until GetFullUser calls may resume (0 when not cooling down)."""
    return _bio_pacer.cooldown_remaining()


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
