"""
Database work must not run on the event loop (R-1).

src/db.py is synchronous psycopg and there was no thread offloading anywhere, so
every one of ~50 helpers executed on the single thread shared by PTB polling, all
PTB handlers, the Pyrogram client and four background tasks. check_user was an
`async def` containing six blocking reads and no await at all.

The worst of it was get_connection's retry ladder: 8 retries with a 30s pool
timeout and time.sleep backoff totalling ~120s of sleep, so an unreachable
database froze the whole process for up to ~6 minutes. From Railway's side the
process looked perfectly healthy — it was alive, in time.sleep. Meanwhile
getUpdates was never issued and Pyrogram could not answer MTProto pings.
"""
import asyncio
import threading
import time

import pytest

from src import db
from src.utils import checker
from src.utils.checker import UserSnapshot, check_user


SLOW = 0.25          # how long the fake DB call blocks
TICK = 0.01          # ticker granularity


def _patch_detection(monkeypatch, slow_fn):
    monkeypatch.setattr(checker, "is_whitelisted", slow_fn)
    monkeypatch.setattr(checker, "is_false_positive", lambda gid, uid: False)
    monkeypatch.setattr(checker, "get_group", lambda gid: None)
    monkeypatch.setattr(checker, "get_known_bad_actor", lambda uid: None)
    monkeypatch.setattr(checker, "get_whitelist", lambda gid: [])
    monkeypatch.setattr(checker, "get_reserved_keywords", lambda gid: [])


def test_check_user_leaves_the_event_loop_responsive(monkeypatch):
    """
    The load-bearing test for R-1. A slow DB read must not stop other tasks —
    PTB's long-poll and Pyrogram's keepalive are exactly those other tasks.
    """
    def slow_is_whitelisted(gid, uid):
        time.sleep(SLOW)          # a real blocking call, as psycopg would be
        return False
    _patch_detection(monkeypatch, slow_is_whitelisted)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(TICK)
            ticks += 1

    async def scenario():
        t = asyncio.create_task(ticker())
        await asyncio.sleep(0)                      # let the ticker start
        snap = UserSnapshot(user_id=1, username=None, first_name="A", last_name=None)
        await check_user(snap, -100)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    # With the read on the loop this is 0-1. Off the loop it should be most of
    # SLOW/TICK (~25); allow generous slack for scheduler jitter and CI load.
    assert ticks >= 5, (
        f"event loop was blocked during check_user: only {ticks} ticks in {SLOW}s"
    )


def test_concurrent_checks_overlap_instead_of_serialising(monkeypatch):
    """Four concurrent detections should not take 4x a single one."""
    def slow_is_whitelisted(gid, uid):
        time.sleep(SLOW)
        return False
    _patch_detection(monkeypatch, slow_is_whitelisted)

    async def scenario():
        snap = UserSnapshot(user_id=1, username=None, first_name="A", last_name=None)
        started = time.monotonic()
        await asyncio.gather(*(check_user(snap, -100 - i) for i in range(4)))
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < SLOW * 3, f"checks serialised on the loop: {elapsed:.2f}s for 4"


# ── get_connection must be time-bounded ───────────────────────────────────────

class _AlwaysFailingPool:
    check_connection = staticmethod(lambda conn: None)

    def __init__(self, exc=None):
        self.attempts = 0
        self._exc = exc or RuntimeError("connection refused")

    def getconn(self, timeout=None):
        self.attempts += 1
        raise self._exc


def test_get_connection_gives_up_within_a_bounded_wall_clock(monkeypatch):
    """
    Asserts the planned sleep budget rather than sleeping for real — the old
    ladder took ~120s of sleep plus up to 8x30s of pool timeout, which would
    make this test itself unusable.
    """
    pool = _AlwaysFailingPool()
    monkeypatch.setattr(db, "_get_pool", lambda: pool)

    slept = []
    monkeypatch.setattr(db.time, "sleep", lambda s: slept.append(s))

    assert db.get_connection() is None
    total = sum(slept)
    assert total <= 15, (
        f"backoff budget is {total:.0f}s across {pool.attempts} attempts; "
        "the old ladder planned ~120s"
    )
    assert pool.attempts <= 4, f"{pool.attempts} attempts is too many to wait through"


def test_configuration_errors_are_not_retried(monkeypatch):
    """Retrying a wrong password just multiplies the delay by 8."""
    import psycopg

    pool = _AlwaysFailingPool(
        psycopg.OperationalError('password authentication failed for user "bot"')
    )
    monkeypatch.setattr(db, "_get_pool", lambda: pool)
    monkeypatch.setattr(db.time, "sleep", lambda s: None)   # don't wait out the old ladder
    assert db.get_connection() is None
    assert pool.attempts == 1, f"retried a fatal config error {pool.attempts} times"


# ── the pool must be built exactly once ───────────────────────────────────────

def test_pool_is_built_once_under_thread_concurrency(monkeypatch):
    """
    Latent before R-1 because everything ran on one thread; live the moment DB
    work moves to a thread pool. Two pools means the loser leaks its background
    worker and putconn hands connections to the wrong owner.
    """
    created = []

    class _FakePool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, **kwargs):
            time.sleep(0.02)          # widen the check-then-act window
            created.append(self)

    monkeypatch.setattr(db, "ConnectionPool", _FakePool)
    monkeypatch.setattr(db, "_pool", None)

    seen = []
    threads = [threading.Thread(target=lambda: seen.append(db._get_pool()))
               for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(created) == 1, f"built {len(created)} pools concurrently"
    assert len({id(p) for p in seen}) == 1, "threads got different pools"
