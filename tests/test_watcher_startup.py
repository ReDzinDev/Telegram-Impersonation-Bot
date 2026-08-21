"""
A broken Pyrogram session must degrade, not kill the bot (R-2).

`await pyro_client.start()` was the only unguarded await in the startup
sequence — the get_dialogs warm-up right below it WAS wrapped. It raises on a
revoked session (AuthKeyUnregistered / SessionRevoked / UserDeactivatedBan), on
a malformed session string, and on a non-integer api_id.

Because it ran AFTER updater.start_polling() and BEFORE the try/finally, the
exception escaped main() with the long-poll still open and nothing calling
updater.stop() — producing exactly the `Conflict: terminated by other getUpdates
request` churn the SIGTERM handling was added to prevent, then a 10x crash-loop
and a permanently dead service.

The painful part: group moderation is all Bot API and would have kept working.
"""
import asyncio

import pytest

from src import main as main_mod


class _FakeClient:
    def __init__(self, start_error=None, dialogs_error=None):
        self._start_error = start_error
        self._dialogs_error = dialogs_error
        self.started = False

    async def start(self):
        if self._start_error:
            raise self._start_error
        self.started = True

    async def get_dialogs(self):
        if self._dialogs_error:
            raise self._dialogs_error
        for _ in ():
            yield _


class _FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kw):
        self.messages.append(text)


def _start(client, bot=None):
    return asyncio.run(main_mod._start_watcher(client, bot, log_channel_id=None))


def test_healthy_client_starts_and_is_returned():
    client = _FakeClient()
    assert _start(client) is client
    assert client.started is True


@pytest.mark.parametrize("error", [
    RuntimeError("AUTH_KEY_UNREGISTERED"),
    ValueError("invalid literal for int()"),
    ConnectionError("network unreachable"),
])
def test_a_failing_client_degrades_to_none_instead_of_raising(error):
    """Bot-API-only operation is the correct fallback, not a crash-loop."""
    assert _start(_FakeClient(start_error=error)) is None


def test_failure_is_reported_to_the_operator():
    bot = _FakeBot()
    result = asyncio.run(main_mod._start_watcher(
        _FakeClient(start_error=RuntimeError("SESSION_REVOKED")),
        bot, log_channel_id="-1001234567890",
    ))
    assert result is None
    assert bot.messages, "operator was never told the watcher is down"
    assert "watcher" in bot.messages[0].lower()


def test_a_warmup_failure_still_yields_a_usable_client():
    """The entity cache is an optimisation; failing to warm it isn't fatal."""
    client = _FakeClient(dialogs_error=RuntimeError("FLOOD_WAIT_5"))
    assert _start(client) is client
