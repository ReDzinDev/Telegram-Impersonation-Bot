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
    p = _Pacer("t", 0.1)
    p.on_flood(1)
    p.on_flood(1)
    assert p.interval > 0.1
    assert p._flood_streak == 2

    p._last_flood = time.monotonic() - (fetch._FORGIVE_AFTER + 1)
    p._maybe_forgive()
    assert p.interval == 0.1
    assert p._flood_streak == 0
