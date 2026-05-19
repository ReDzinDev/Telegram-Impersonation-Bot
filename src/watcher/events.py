
"""
Pyrogram raw-update handlers for profile changes.

UpdateUserName fires when any Telegram user changes their first name,
last name, or username. UpdateUserPhoto fires on profile picture changes.
Both are MTProto-only — the Bot API has no equivalent event.

When a change is detected for a user who is a member of one of our
monitored groups, we:
  1. Unmark them as "seen" so RELAXED-mode re-checks them on next message.
  2. Run a full impersonation check immediately.
  3. Ban + log if flagged.
"""
from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from typing import TYPE_CHECKING

from pyrogram import Client, raw
from telegram import Bot

from src.db import get_all_group_ids, get_groups_for_user, unmark_seen
from src.utils.checker import UserSnapshot, check_user, ban_and_log

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def register_event_handlers(pyro: Client, bot: Bot, log_channel_id: str | None):
    """Attach raw-update handlers to the Pyrogram client."""

    # UpdateUserPhoto was removed in newer Telegram API layers; guard with getattr
    _UpdateUserPhoto = getattr(raw.types, "UpdateUserPhoto", None)

    @pyro.on_raw_update()
    async def on_raw_update(client: Client, update, users, chats):
        if isinstance(update, raw.types.UpdateUserName):
            await _handle_name_change(client, bot, update, log_channel_id)
        elif _UpdateUserPhoto and isinstance(update, _UpdateUserPhoto):
            await _handle_photo_change(client, bot, update, log_channel_id)


async def _handle_name_change(
    pyro: Client, bot: Bot,
    update: raw.types.UpdateUserName,
    log_channel_id: str | None,
):
    user_id = update.user_id

    # Resolve which of our groups this user belongs to
    group_ids = get_groups_for_user(user_id)
    if not group_ids:
        return

    # Build new name from the update payload
    first_name = update.first_name or ""
    last_name = update.last_name or ""
    username = update.usernames[0].username if update.usernames else None

    logger.info(f"Profile name change detected for user {user_id}: {first_name} {last_name} @{username}")

    # Invalidate seen cache so RELAXED mode re-checks on next message
    for gid in group_ids:
        unmark_seen(gid, user_id)

    # Fetch current PFP for a full check
    pfp_bytes = await _fetch_pfp(pyro, user_id)

    snapshot = UserSnapshot(
        user_id=user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        pfp_bytes=pfp_bytes,
    )

    await _check_and_act(pyro, bot, snapshot, group_ids, trigger="profile_change", log_channel_id=log_channel_id)


async def _handle_photo_change(
    pyro: Client, bot: Bot,
    update: raw.types.UpdateUserPhoto,
    log_channel_id: str | None,
):
    user_id = update.user_id

    group_ids = get_groups_for_user(user_id)
    if not group_ids:
        return

    logger.info(f"Profile photo change detected for user {user_id}")

    for gid in group_ids:
        unmark_seen(gid, user_id)

    # Resolve full user info to get current name too
    try:
        peer = await pyro.resolve_peer(user_id)
        users_info = await pyro.invoke(
            raw.functions.users.GetUsers(id=[peer])
        )
        user = users_info[0] if users_info else None
    except Exception as e:
        logger.warning(f"Could not resolve user {user_id} for photo change: {e}")
        user = None

    first_name = getattr(user, "first_name", "") or ""
    last_name = getattr(user, "last_name", "") or ""
    username = None
    if user and getattr(user, "usernames", None):
        username = user.usernames[0].username
    elif user:
        username = getattr(user, "username", None)

    pfp_bytes = await _fetch_pfp(pyro, user_id)

    snapshot = UserSnapshot(
        user_id=user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        pfp_bytes=pfp_bytes,
    )

    await _check_and_act(pyro, bot, snapshot, group_ids, trigger="profile_change", log_channel_id=log_channel_id)


async def _check_and_act(
    pyro: Client, bot: Bot,
    snapshot: UserSnapshot,
    group_ids: list[int],
    trigger: str,
    log_channel_id: str | None,
):
    for group_id in group_ids:
        result = await check_user(snapshot, group_id)
        if not result.flagged:
            continue

        async def _ban(gid: int, uid: int):
            await bot.ban_chat_member(chat_id=gid, user_id=uid)

        log_notify = None
        if log_channel_id:
            async def log_notify(text: str, markup=None, _lcid=log_channel_id):
                await bot.send_message(chat_id=_lcid, text=text, parse_mode="HTML", reply_markup=markup)

        async def _unban(gid: int, uid: int):
            await bot.unban_chat_member(chat_id=gid, user_id=uid)

        await ban_and_log(
            result=result,
            snapshot=snapshot,
            group_id=group_id,
            trigger=trigger,
            ban_func=_ban,
            unban_func=_unban,
            log_channel_notify=log_notify,
        )


async def _fetch_pfp(pyro: Client, user_id: int) -> bytes | None:
    try:
        buf = BytesIO()
        async for chunk in pyro.stream_media(
            await pyro.get_chat_photos(user_id, limit=1).__anext__()
        ):
            buf.write(chunk)
        return buf.getvalue() or None
    except StopAsyncIteration:
        return None
    except Exception as e:
        logger.debug(f"Could not fetch PFP for {user_id}: {e}")
        return None
