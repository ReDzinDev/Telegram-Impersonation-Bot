"""
A group must be usable as a log destination, not only a channel.

The A-3 fix (rights on the TARGET are what authorise a binding) also implemented
the audit's secondary suggestion — "reject targets whose chat.type isn't
CHANNEL". That was belt-and-braces, and it removed a legitimate setup: a private
group as the log destination, which is what many operators actually use.

The security property worth keeping is the rights check. Whether the target is a
channel or a group, the caller must be an admin THERE — otherwise anyone could
redirect their group's alerts (and /clearwhitelist's whitelist CSV) into someone
else's chat. That check generalises unchanged; only the type gate needed widening.
"""
import asyncio
import re
from pathlib import Path

import pytest
from telegram.constants import ChatMemberStatus, ChatType

from src.handlers.commands import _verify_log_channel_target

ROOT = Path(__file__).resolve().parent.parent
TARGET = -1001111111111
CALLER = 4242


class _Member:
    def __init__(self, status):
        self.status = status


class _Chat:
    def __init__(self, chat_type):
        self.type = chat_type
        self.title = "Some Chat"


class _Bot:
    def __init__(self, chat_type, status=ChatMemberStatus.ADMINISTRATOR):
        self._chat_type = chat_type
        self._status = status

    async def get_chat(self, chat_id):
        return _Chat(self._chat_type)

    async def get_chat_member(self, chat_id, user_id):
        return _Member(self._status)


class _Ctx:
    def __init__(self, bot):
        self.bot = bot


def _verify(chat_type, status=ChatMemberStatus.ADMINISTRATOR):
    return asyncio.run(
        _verify_log_channel_target(_Ctx(_Bot(chat_type, status)), TARGET, CALLER)
    )


# ── the types that must now work ──────────────────────────────────────────────

@pytest.mark.parametrize("chat_type", [
    ChatType.CHANNEL,
    ChatType.GROUP,
    ChatType.SUPERGROUP,
])
def test_channels_and_groups_are_both_accepted(chat_type):
    ok, err = _verify(chat_type)
    assert ok is True, f"{chat_type} rejected: {err}"
    assert err is None


def test_a_private_chat_is_still_refused():
    """
    A DM has no admins, so "are you an admin of the target?" is meaningless
    there — the rights check that makes this safe cannot be applied.
    """
    ok, err = _verify(ChatType.PRIVATE)
    assert ok is False
    assert err and ("group" in err.lower() or "channel" in err.lower())


# ── the property that must survive the widening ───────────────────────────────

@pytest.mark.parametrize("chat_type", [
    ChatType.CHANNEL,
    ChatType.GROUP,
    ChatType.SUPERGROUP,
])
def test_a_non_admin_of_the_target_is_still_refused(chat_type):
    """The whole point of A-3: you cannot redirect alerts into someone else's chat."""
    ok, err = _verify(chat_type, status=ChatMemberStatus.MEMBER)
    assert ok is False
    assert "admin" in err.lower()


@pytest.mark.parametrize("chat_type", [ChatType.GROUP, ChatType.SUPERGROUP])
def test_an_owner_of_a_group_target_is_accepted(chat_type):
    ok, err = _verify(chat_type, status=ChatMemberStatus.OWNER)
    assert ok is True, err


# ── the picker has to offer groups too ────────────────────────────────────────
#
# KeyboardButtonRequestChat takes a single chat_is_channel bool, so one button
# cannot mean "channel or group" — groups need their own button and request_id.

def _commands_source() -> str:
    return (ROOT / "src" / "handlers" / "commands.py").read_text(encoding="utf-8")


def test_the_picker_offers_a_group_button():
    source = _commands_source()
    assert "chat_is_channel=False" in source, (
        "no group button — KeyboardButtonRequestChat(chat_is_channel=False)"
    )
    assert "chat_is_channel=True" in source, "the channel button disappeared"


def test_the_group_button_uses_its_own_request_id():
    source = _commands_source()
    ids = set(re.findall(r"request_id=(\d+)", source))
    assert {"1", "2", "3"} <= ids, f"expected request ids 1,2,3; found {sorted(ids)}"


def test_both_log_target_request_ids_are_handled():
    """A button that produces an unhandled request_id silently does nothing."""
    source = _commands_source()
    handler = source[source.index("async def handle_chat_shared"):]
    handler = handler[:handler.index("\nasync def ")]
    assert re.search(r"request_id in \(2, 3\)|request_id in \(3, 2\)", handler), (
        "handle_chat_shared does not dispatch both log-target request ids"
    )


# ── the footgun that allowing groups opens up ─────────────────────────────────

def test_pointing_a_group_at_itself_is_warned_about():
    """
    Now that a group is a valid destination, the monitored group is one too — and
    then the person being flagged reads their own detection alert, and sees the
    whitelist CSV that /clearwhitelist posts. Worth warning about; not worth
    blocking, since an operator may genuinely want alerts in-channel.
    """
    from src.handlers.commands import log_target_warning

    warning = log_target_warning(group_id=-100123, target_id=-100123)
    assert warning, "no warning when alerts point at the monitored group itself"
    assert "same" in warning.lower() or "itself" in warning.lower()


def test_a_separate_destination_is_not_warned_about():
    from src.handlers.commands import log_target_warning

    assert log_target_warning(group_id=-100123, target_id=-100999) == ""
