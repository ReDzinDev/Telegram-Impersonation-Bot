"""
A dead background task must be restarted, not merely reported (R-6).

_supervise attached a done-callback that logged at ERROR and posted to the log
channel. Genuinely useful, but it never restarted anything: if
run_periodic_sweeps died, scheduled sweeps stopped FOREVER while the bot carried
on serving commands as though healthy. Recovery depended on a human noticing one
Telegram message.

Nothing detected the other failure shape either — an event loop that is alive but
stalled. That is what a blocking call sneaking back into an async path looks like
from the outside (R-1's signature), and it is invisible to a liveness check that
only asks "is the process running?".
"""
import asyncio
import logging

import pytest

from src import main as main_mod


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """Real constants are 30s..10min; tests need them small."""
    monkeypatch.setattr(main_mod, "_SUPERVISOR_BASE_DELAY", 0.01)
    monkeypatch.setattr(main_mod, "_SUPERVISOR_MAX_DELAY", 0.08)
    monkeypatch.setattr(main_mod, "_SUPERVISOR_HEALTHY_AFTER", 0.5)


async def _run_until(calls: list, target: int, coro, timeout=3.0):
    """Drive `coro` as a task until the factory has been called `target` times."""
    task = asyncio.create_task(coro)
    waited = 0.0
    while len(calls) < target and waited < timeout:
        await asyncio.sleep(0.01)
        waited += 0.01
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return len(calls)


def test_a_crashing_task_is_restarted():
    calls = []

    async def factory():
        calls.append(1)
        raise RuntimeError("boom")

    got = asyncio.run(_run_until(calls, 3, main_mod._supervised("t", factory)))
    assert got >= 3, f"restarted only {got} time(s) — a dead task stays dead"


def test_a_task_that_returns_cleanly_is_also_restarted():
    """Its body is a `while True`; returning at all is itself a bug."""
    calls = []

    async def factory():
        calls.append(1)
        return

    got = asyncio.run(_run_until(calls, 3, main_mod._supervised("t", factory)))
    assert got >= 3


def test_backoff_grows_between_restarts(monkeypatch):
    delays = []
    real_sleep = asyncio.sleep

    async def recording_sleep(d):
        delays.append(d)
        await real_sleep(0)          # yield without waiting
    monkeypatch.setattr(asyncio, "sleep", recording_sleep)

    calls = []

    async def factory():
        calls.append(1)
        raise RuntimeError("boom")

    async def scenario():
        task = asyncio.create_task(main_mod._supervised("t", factory))
        while len(calls) < 5:
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    backoffs = [d for d in delays if d > 0]
    assert backoffs, "no backoff between restarts"
    assert backoffs != sorted(backoffs, reverse=True), "backoff never grows"
    assert max(backoffs) <= main_mod._SUPERVISOR_MAX_DELAY, "backoff is uncapped"


def test_backoff_resets_after_a_healthy_run(monkeypatch):
    """
    A task that ran for hours and then died should not restart at max backoff.
    """
    monkeypatch.setattr(main_mod, "_SUPERVISOR_HEALTHY_AFTER", 0.02)
    delays = []
    real_sleep = asyncio.sleep

    async def recording_sleep(d):
        delays.append(d)
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", recording_sleep)

    calls = []

    async def factory():
        calls.append(1)
        # Long-lived on the first two runs, instant afterwards.
        if len(calls) <= 2:
            await real_sleep(0.05)
        raise RuntimeError("boom")

    async def scenario():
        task = asyncio.create_task(main_mod._supervised("t", factory))
        while len(calls) < 4:
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    backoffs = [d for d in delays if d > 0]
    assert backoffs[:2] == [main_mod._SUPERVISOR_BASE_DELAY] * 2, (
        f"a healthy run did not reset the backoff: {backoffs}"
    )


def test_cancellation_stops_the_supervisor():
    """Shutdown must not be fought by the restart loop."""
    calls = []

    async def factory():
        calls.append(1)
        await asyncio.sleep(10)

    async def scenario():
        task = asyncio.create_task(main_mod._supervised("t", factory))
        while not calls:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_the_operator_is_told_each_time(caplog):
    calls = []

    async def factory():
        calls.append(1)
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        asyncio.run(_run_until(calls, 2, main_mod._supervised("periodic_sweep", factory)))
    assert any("periodic_sweep" in r.message for r in caplog.records), (
        "the death was not logged at ERROR"
    )


def test_a_notifier_is_invoked_on_death():
    calls, notices = [], []

    async def factory():
        calls.append(1)
        raise RuntimeError("boom")

    async def notify(name, exc):
        notices.append((name, exc))

    asyncio.run(_run_until(
        calls, 2, main_mod._supervised("sweep", factory, notify=notify)
    ))
    assert notices, "the operator notifier was never called"
    assert notices[0][0] == "sweep"


# ── stalled-but-alive detection ───────────────────────────────────────────────

def test_the_watchdog_reports_a_stalled_loop(caplog):
    """
    The only cheap detector for a blocking call sneaking back into an async path.
    A process frozen in time.sleep looks perfectly healthy from outside.
    """
    async def scenario():
        task = asyncio.create_task(
            main_mod._loop_lag_watchdog(threshold=0.05, interval=0.01)
        )
        await asyncio.sleep(0.02)
        import time as _time
        _time.sleep(0.2)             # a real, blocking stall
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert any("stall" in r.message.lower() for r in caplog.records), (
        "a 200ms blocking stall was not reported"
    )


def test_the_watchdog_is_quiet_when_the_loop_is_healthy(caplog):
    async def scenario():
        task = asyncio.create_task(
            main_mod._loop_lag_watchdog(threshold=0.5, interval=0.01)
        )
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert not [r for r in caplog.records if "stall" in r.message.lower()], (
        "the watchdog cried wolf on a healthy loop"
    )
