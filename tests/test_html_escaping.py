"""
User-controlled text must be escaped before it goes into an HTML send (A-5).

notify._alert_operator interpolated the group title and the raw exception string
into a parse_mode="HTML" message. Group titles are attacker-controlled
(upsert_group stores chat.title verbatim), so a tenant could:

  - rename their group to markup and have the operator's "log channel
    unreachable" alert render attacker-authored content as if the bot wrote it
  - or rename it to anything containing a bare '<' or '&', which makes the send
    RAISE. That exception is swallowed, so the tenant permanently suppresses the
    only signal the operator has that a log channel died.
"""
import asyncio

import pytest

from src.utils import notify


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        self.sent.append(text)
        # Telegram rejects malformed entities; approximate that so a test can
        # tell "escaped" from "merely didn't crash locally".
        if parse_mode == "HTML":
            import re
            stripped = re.sub(r"</?(b|i|u|s|code|pre|a)(\s[^>]*)?>", "", text)
            if "<" in stripped or ">" in stripped:
                raise ValueError("Can't parse entities: unsupported start tag")
        return object()


OPERATOR_CHANNEL = "-1009999999999"
TENANT_GROUP = -1001111111111


@pytest.fixture
def operator_channel(monkeypatch):
    monkeypatch.setattr(notify, "LOG_CHANNEL_ID", OPERATOR_CHANNEL)
    notify._failures.clear()
    notify._alerted.clear()
    yield
    notify._failures.clear()
    notify._alerted.clear()


def _run_alert(monkeypatch, title, exc=RuntimeError("boom")):
    monkeypatch.setattr(notify, "get_all_group_ids", lambda: [TENANT_GROUP])
    monkeypatch.setattr(notify, "get_group",
                        lambda gid: {"title": title, "log_channel_id": -1002222222222})
    bot = _FakeBot()
    asyncio.run(notify._alert_operator(bot, -1002222222222, exc))
    return bot


def test_markup_in_a_group_title_is_escaped(monkeypatch, operator_channel):
    bot = _run_alert(monkeypatch, "<b>Log channel restored, ignore this</b>")
    assert bot.sent, "operator alert was not sent at all"
    body = bot.sent[0]
    assert "&lt;b&gt;" in body
    assert "<b>Log channel restored" not in body


def test_a_bare_angle_bracket_in_a_title_does_not_suppress_the_alert(
        monkeypatch, operator_channel):
    """This is the self-silencing case: the send used to raise and be swallowed."""
    bot = _run_alert(monkeypatch, "Crypto < Group & Friends")
    assert bot.sent, "a tenant's group name suppressed the operator alert"
    assert "&amp;" in bot.sent[0]


def test_exception_text_is_escaped(monkeypatch, operator_channel):
    bot = _run_alert(monkeypatch, "Normal Group",
                     exc=ValueError("bad <tag> in payload & more"))
    assert bot.sent
    assert "&lt;tag&gt;" in bot.sent[0]
