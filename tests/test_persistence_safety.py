"""
PTB persistence must be able to snapshot bot_data (B-1).

Application.update_persistence() documents the requirement plainly: "Any data is
deep copied with copy.deepcopy before handing it over to the persistence ... so
all persisted data must be copyable." Its private path builds the coroutine set
with update_bot_data(deepcopy(self.bot_data)) BEFORE adding the chat_data and
user_data entries, and outside the gather(return_exceptions=True) that would have
absorbed a failure.

A Pyrogram Client cannot be deep-copied (TypeError: cannot pickle
'_queue.SimpleQueue' object). Storing one in bot_data therefore killed the
persistence updater on its first tick, so NOTHING was ever persisted — and
Application.stop() awaits that dead task as its documented last step, re-raising
the TypeError so shutdown() never ran and SIGTERM exited non-zero.

The visible symptom was every admin having to re-run /start after each redeploy,
because active_group_id lives only in that never-written file.
"""
import asyncio
import copy
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def current_event_loop():
    """
    Pyrogram's Dispatcher.__init__ calls asyncio.get_event_loop(), which raises
    on Python 3.11+ once an earlier test's asyncio.run() has closed and cleared
    the loop. Production never hits this — build_client() runs inside main()'s
    live loop — so this is purely a harness concern for constructing a Client
    outside one.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_pyrogram_client_is_genuinely_not_deep_copyable(current_event_loop):
    """Pins the premise. If this ever stops failing, the guard below is moot."""
    from pyrogram import Client

    client = Client("probe", api_id=12345, api_hash="0" * 32, in_memory=True)
    # Pyrogram raises TypeError today ("cannot pickle '_queue.SimpleQueue'"), but
    # the guarantee we depend on is only "not deep-copyable" — pinning the exact
    # type would make this brittle across Pyrogram versions.
    with pytest.raises((TypeError, ValueError, RecursionError)):
        copy.deepcopy(client)


def test_no_module_puts_the_pyrogram_client_into_bot_data():
    """
    bot_data is snapshotted by deepcopy every update_interval. The client is
    reachable via src.watcher.client.get_client(), which needs no pickling, so
    nothing should ever be stored there.
    """
    offenders = []
    pattern = re.compile(r"""bot_data\s*\[\s*["']pyro_client["']\s*\]\s*=""")
    for path in SRC.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(SRC.parent)}:{lineno}: {line.strip()}")
    assert offenders == [], "unpicklable client assigned into bot_data:\n" + "\n".join(offenders)


def test_built_application_has_deep_copyable_bot_data(monkeypatch, tmp_path):
    """Exercises exactly what PTB's persistence updater does every interval."""
    monkeypatch.chdir(tmp_path)          # keep PicklePersistence off the repo file
    from src.main import build_ptb_app

    app = build_ptb_app()
    app.bot_data["log_channel_id"] = "-1001234567890"
    copy.deepcopy(app.bot_data)          # must not raise


def test_get_client_returns_the_built_client(current_event_loop):
    """The replacement for bot_data lookup."""
    from src.watcher import client as client_mod

    previous = client_mod.get_client()
    try:
        built = client_mod.build_client("12345", "0" * 32, "")
        assert client_mod.get_client() is built
    finally:
        client_mod._client = previous
