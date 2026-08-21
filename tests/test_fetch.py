"""
Tests for the adaptive MTProto pacer in src/watcher/fetch.py.

Pure-logic tests — no network, no Pyrogram client. The one time-sensitive
check uses a generous margin so it stays stable on slow machines.
"""
import asyncio
import time

import pytest

from src.watcher import fetch
from src.watcher.fetch import _Pacer


def test_acquire_enforces_min_interval():
    async def run():
        p = _Pacer("t", 0.15)
        assert await p.acquire(max_wait=5)
        t0 = time.monotonic()
        assert await p.acquire(max_wait=5)
        return time.monotonic() - t0

    elapsed = asyncio.run(run())
    assert elapsed >= 0.12  # ~interval, allowing timer slop


def test_flood_sets_cooldown_with_padding():
    p = _Pacer("t", 0.1)
    total = p.on_flood(10)
    assert total == 15  # 10 mandated + 5 base padding
    assert 14 < p.cooldown_remaining() <= 15
    assert p.interval == pytest.approx(0.15)


def test_padding_escalates_and_caps():
    p = _Pacer("t", 0.1)
    assert p.on_flood(1) == 1 + 5
    assert p.on_flood(1) == 1 + 10
    assert p.on_flood(1) == 1 + 20
    assert p.on_flood(1) == 1 + 40
    assert p.on_flood(1) == 1 + 60
    assert p.on_flood(1) == 1 + 60  # padding capped at 60s


def test_interval_ratchet_caps():
    p = _Pacer("t", 8.0)
    p.on_flood(1)
    assert p.interval == 10.0  # 8 * 1.5 = 12 → capped


def test_acquire_skips_when_cooldown_exceeds_max_wait():
    async def run():
        p = _Pacer("t", 0.05)
        p.on_flood(30)  # 35s total cooldown
        return await p.acquire(max_wait=10)

    assert asyncio.run(run()) is False


def test_forgiveness_resets_pacing_after_quiet_period():
    """
    A genuine quiet period: the cooldown has expired AND _FORGIVE_AFTER has
    elapsed since. Backdating only _last_flood (as this test used to) leaves
    _flood_until in the future, which is the very case forgiveness must refuse.
    """
    p = _Pacer("t", 0.1)
    p.on_flood(1)
    p.on_flood(1)
    assert p.interval > 0.1
    assert p._flood_streak == 2

    past = time.monotonic() - (fetch._FORGIVE_AFTER + 1)
    p._last_flood = past
    p._flood_until = past          # the cooldown really is over
    p._maybe_forgive()
    assert p.interval == 0.1
    assert p._flood_streak == 0


# ── R-3: forgiveness must not fire during an active cooldown ──────────────────

def test_forgiveness_refuses_while_a_cooldown_is_still_active():
    """
    _maybe_forgive measured quiet time from when the flood was RECORDED, not
    from when the cooldown ENDS. on_flood honours arbitrarily long mandated
    waits, so any FloodWait longer than _FORGIVE_AFTER (600s) guaranteed the
    pacer forgot everything while still cooling down — resuming at full base
    speed with a virgin escalation ladder, in exactly the severe case the
    ratchet exists for.
    """
    p = _Pacer("t", 1.2)
    p.on_flood(3600)                      # one-hour FloodWait
    p.on_flood(3600)
    ratcheted = p.interval
    assert ratcheted > 1.2

    # 601s of quiet since the flood was recorded, but the cooldown runs for an
    # hour, so we are still inside it.
    p._last_flood = time.monotonic() - (fetch._FORGIVE_AFTER + 1)
    p._maybe_forgive()

    assert p.cooldown_remaining() > 0, "precondition: still cooling down"
    assert p.interval == ratcheted, "un-ratcheted mid-cooldown"
    assert p._flood_streak == 2, "escalation ladder was reset mid-cooldown"


def test_forgiveness_fires_once_the_cooldown_has_also_elapsed():
    p = _Pacer("t", 1.2)
    p.on_flood(5)
    past = time.monotonic() - (fetch._FORGIVE_AFTER + 10)
    p._last_flood = past
    p._flood_until = past
    p._maybe_forgive()
    assert p.interval == 1.2
    assert p._flood_streak == 0


# ── R-3: max_wait must be a real bound ────────────────────────────────────────

def test_max_wait_bounds_every_granted_caller():
    """
    acquire() slept INSIDE the lock and _pending_wait() only saw _next_slot,
    never lock queue depth. With N callers queued the Nth waited
    (N-1) * interval while the ceiling check believed the wait was one interval.
    """
    max_wait = 0.25

    async def run():
        p = _Pacer("t", 0.2)
        started = time.monotonic()

        async def caller():
            granted = await p.acquire(max_wait=max_wait)
            return granted, time.monotonic() - started

        return await asyncio.gather(*(caller() for _ in range(6)))

    results = asyncio.run(run())
    for granted, waited in results:
        if granted:
            assert waited <= max_wait + 0.15, (
                f"granted caller waited {waited:.2f}s against a {max_wait}s ceiling"
            )
    assert any(g for g, _ in results), "nobody got through at all"


def test_granted_calls_are_still_spaced_by_the_interval():
    """The ceiling fix must not turn the pacer into a free-for-all."""
    async def run():
        p = _Pacer("t", 0.15)
        stamps = []

        async def caller():
            if await p.acquire(max_wait=5):
                stamps.append(time.monotonic())

        await asyncio.gather(*(caller() for _ in range(4)))
        return sorted(stamps)

    stamps = asyncio.run(run())
    assert len(stamps) == 4
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert all(g >= 0.12 for g in gaps), f"calls bunched up: {gaps}"


def test_a_flood_recorded_while_waiting_is_respected():
    """
    The RPC happens outside the lock, so a caller could be mid-sleep when
    another caller's in-flight call records a flood. It then woke with stale
    state and fired straight into the cooldown, earning its own FloodWait and
    adding a rung to the escalation ladder.
    """
    async def run():
        p = _Pacer("t", 0.3)
        assert await p.acquire(max_wait=5)          # take the first slot

        async def late_flood():
            await asyncio.sleep(0.05)
            p.on_flood(30)                          # 35s cooldown lands mid-wait

        flood_task = asyncio.create_task(late_flood())
        granted = await p.acquire(max_wait=1.0)
        await flood_task
        return granted, p.cooldown_remaining()

    granted, cooldown = asyncio.run(run())
    assert cooldown > 0, "precondition: a cooldown is active"
    assert granted is False, "fired into an active flood cooldown"


# ── E-6: FloodWaits must reach the pacer ──────────────────────────────────────

def test_client_surfaces_every_floodwait():
    """
    Session.SLEEP_THRESHOLD defaults to 10, and Session.invoke SWALLOWS any
    FloodWait at or under it — sleeping internally and retrying, raising
    nothing. So every early, mild pushback was invisible: on_flood never ran,
    the interval never ratcheted, and the pacer reported a healthy system while
    Telegram was already throttling us. That is the opposite of what an adaptive
    pacer is for.

    Note a per-call sleep_threshold=0 does NOT work — invoke() treats falsy as
    unset — so it has to be set on the Client.
    """
    import inspect

    from src.watcher import client as client_mod

    src = inspect.getsource(client_mod.build_client)
    assert "sleep_threshold=0" in src, (
        "build_client must set sleep_threshold=0 or mild FloodWaits never "
        "reach the pacer"
    )
