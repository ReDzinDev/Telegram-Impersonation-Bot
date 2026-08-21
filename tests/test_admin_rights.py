"""
Admin authority must reflect granted rights, and demotion must take effect (A-4).

Two problems:

  - the check was `member.status in [ADMINISTRATOR, OWNER]` with no inspection of
    granted rights, so a decorative admin promoted with zero permissions (or only
    can_pin_messages) could drive the BOT's ban rights via /ban, /clearwhitelist
    and the alert buttons — bypassing the group's intended delegation
  - only positive results were cached and nothing ever invalidated them, so a
    demoted admin, or one who left, kept full authority for up to 300s. The dict
    also grew one entry per (user, group) pair ever seen
"""
import time

from telegram.constants import ChatMemberStatus

from src.handlers import commands


class _Member:
    def __init__(self, status, **rights):
        self.status = status
        for k, v in rights.items():
            setattr(self, k, v)


# ── rights derivation ─────────────────────────────────────────────────────────

def test_owner_has_full_authority():
    is_admin, can_moderate = commands._member_rights(_Member(ChatMemberStatus.OWNER))
    assert (is_admin, can_moderate) == (True, True)


def test_admin_with_restrict_rights_may_moderate():
    m = _Member(ChatMemberStatus.ADMINISTRATOR, can_restrict_members=True)
    assert commands._member_rights(m) == (True, True)


def test_decorative_admin_may_configure_but_not_moderate():
    """The core issue: zero-rights admin wielding the bot's ban powers."""
    m = _Member(ChatMemberStatus.ADMINISTRATOR, can_restrict_members=False,
                can_pin_messages=True)
    is_admin, can_moderate = commands._member_rights(m)
    assert is_admin is True, "still an admin for configuration purposes"
    assert can_moderate is False, "must not be able to ban through the bot"


def test_admin_with_no_rights_attribute_at_all_cannot_moderate():
    """Fail closed if the field is absent from the payload."""
    m = _Member(ChatMemberStatus.ADMINISTRATOR)
    assert commands._member_rights(m) == (True, False)


def test_plain_member_has_no_authority():
    assert commands._member_rights(_Member(ChatMemberStatus.MEMBER)) == (False, False)


def test_left_and_banned_members_have_no_authority():
    for status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        assert commands._member_rights(_Member(status)) == (False, False)


# ── cache invalidation ────────────────────────────────────────────────────────

def test_invalidating_drops_the_cached_entry():
    commands._admin_cache[(4242, -100123)] = (time.monotonic() + 300, True, True)
    commands.invalidate_admin_cache(4242, -100123)
    assert (4242, -100123) not in commands._admin_cache


def test_invalidating_an_absent_entry_is_harmless():
    commands._admin_cache.pop((1, 2), None)
    commands.invalidate_admin_cache(1, 2)      # must not raise


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.is_bot = False
        self.username = "demoted"
        self.first_name = "Demoted"
        self.last_name = None
        self.full_name = "Demoted"


class _FakeChatMember:
    def __init__(self, status, user):
        self.status = status
        self.user = user


class _FakeChatMemberUpdate:
    def __init__(self, old_status, new_status, user):
        self.old_chat_member = _FakeChatMember(old_status, user)
        self.new_chat_member = _FakeChatMember(new_status, user)
        self.invite_link = None


class _FakeChat:
    def __init__(self, cid):
        self.id = cid
        self.title = "Some Group"


class _FakeUpdate:
    def __init__(self, cm, chat):
        self.chat_member = cm
        self.effective_chat = chat


def test_demotion_update_evicts_the_cached_privilege():
    """
    Exercises the handler, not just the helper: a demotion arrives as a
    CHAT_MEMBER update, and without eviction the demoted admin keeps /ban and
    /clearwhitelist for the rest of the TTL.

    ADMINISTRATOR -> MEMBER returns early (it is neither a promotion nor a
    fresh join), so nothing after the eviction needs a database.
    """
    import asyncio

    from src.handlers import member_join

    user = _FakeUser(4242)
    group_id = -100123
    commands._admin_cache[(4242, group_id)] = (time.monotonic() + 300, True, True)

    update = _FakeUpdate(
        _FakeChatMemberUpdate(ChatMemberStatus.ADMINISTRATOR,
                              ChatMemberStatus.MEMBER, user),
        _FakeChat(group_id),
    )
    asyncio.run(member_join.check_impersonation(update, context=None))

    assert (4242, group_id) not in commands._admin_cache


def test_unchanged_status_leaves_the_cache_alone():
    """A non-status CHAT_MEMBER update shouldn't cost an API round-trip later."""
    import asyncio

    from src.handlers import member_join

    user = _FakeUser(4242)
    group_id = -100123
    entry = (time.monotonic() + 300, True, True)
    commands._admin_cache[(4242, group_id)] = entry

    update = _FakeUpdate(
        _FakeChatMemberUpdate(ChatMemberStatus.ADMINISTRATOR,
                              ChatMemberStatus.ADMINISTRATOR, user),
        _FakeChat(group_id),
    )
    asyncio.run(member_join.check_impersonation(update, context=None))

    assert commands._admin_cache.get((4242, group_id)) == entry
