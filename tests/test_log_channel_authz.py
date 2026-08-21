"""
Binding a log channel requires rights on the TARGET channel (A-3).

The only gate was "the bot can post there". The chat_shared path verified the
caller admins the *active group* but never that they hold any rights in the
shared chat, and never that it is actually a channel — chat_is_channel=True is a
client-side hint on a payload the code's own comments call untrusted.

So an admin of a throwaway group could point that group's log channel at any
other chat the bot is in. Detection alerts land there with live moderation
buttons, and so does /clearwhitelist's CSV backup, which contains every
protected user's id, username and name.
"""
import asyncio

import pytest
from telegram.constants import ChatMemberStatus, ChatType

from src.handlers.commands import _verify_log_channel_target


CHANNEL = -1001111111111
CALLER = 4242


class _Member:
    def __init__(self, status):
        self.status = status


class _Chat:
    def __init__(self, chat_type):
        self.type = chat_type


class _Bot:
    def __init__(self, chat_type=ChatType.CHANNEL, status=ChatMemberStatus.ADMINISTRATOR,
                 member_error=None, chat_error=None):
        self._chat_type = chat_type
        self._status = status
        self._member_error = member_error
        self._chat_error = chat_error

    async def get_chat(self, chat_id):
        if self._chat_error:
            raise self._chat_error
        return _Chat(self._chat_type)

    async def get_chat_member(self, chat_id, user_id):
        if self._member_error:
            raise self._member_error
        return _Member(self._status)


class _Ctx:
    def __init__(self, bot):
        self.bot = bot


def _verify(bot):
    return asyncio.run(_verify_log_channel_target(_Ctx(bot), CHANNEL, CALLER))


def test_channel_admin_may_bind_it():
    ok, err = _verify(_Bot())
    assert ok is True and err is None


def test_channel_owner_may_bind_it():
    ok, err = _verify(_Bot(status=ChatMemberStatus.OWNER))
    assert ok is True and err is None


def test_non_admin_of_the_target_is_refused():
    """The core exfiltration path: pointing your group's alerts at someone else's chat."""
    ok, err = _verify(_Bot(status=ChatMemberStatus.MEMBER))
    assert ok is False
    assert "admin" in err.lower()


def test_a_group_is_not_a_valid_log_channel():
    ok, err = _verify(_Bot(chat_type=ChatType.SUPERGROUP))
    assert ok is False
    assert "channel" in err.lower()


def test_unreadable_membership_is_refused_not_assumed():
    ok, err = _verify(_Bot(member_error=RuntimeError("Member list is inaccessible")))
    assert ok is False


def test_unreachable_chat_is_refused():
    ok, err = _verify(_Bot(chat_error=RuntimeError("Chat not found")))
    assert ok is False
