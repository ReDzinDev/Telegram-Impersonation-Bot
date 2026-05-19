
"""
Full group member sweep via Pyrogram.

Iterates every member of a monitored group and runs impersonation checks.
The Bot API cannot enumerate supergroup members — this is the MTProto advantage.

Called from:
  - /sweep command (on-demand, triggered via PTB)
  - Periodic background task (every SWEEP_INTERVAL_HOURS hours)
"""
from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from typing import Optional

from pyrogram import Client
from pyrogram.errors import FloodWait, ChatAdminRequired, UserNotParticipant
from telegram import Bot

from src.db import get_all_group_ids, get_whitelist, is_whitelisted, mark_seen, upsert_whitelisted_user
from src.utils.checker import UserSnapshot, check_user, ban_and_log
from src.utils.image import compute_pfp_hash_bytes

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_HOURS = 6
_sweep_locks: dict[int, asyncio.Lock] = {}


async def sweep_group(
    pyro: Client,
    bot: Bot,
    group_id: int,
    log_channel_id: Optional[str] = None,
    progress_cb=None,
) -> dict:
    """
    Sweep all members of group_id.
    progress_cb(checked, flagged, total) — optional callback for live updates.
    Returns a summary dict.
    """
    if group_id not in _sweep_locks:
        _sweep_locks[group_id] = asyncio.Lock()

    if _sweep_locks[group_id].locked():
        return {"status": "already_running"}

    async with _sweep_locks[group_id]:
        checked = 0
        flagged = 0
        errors = 0

        try:
            async for member in pyro.get_chat_members(group_id):
                user = member.user
                if not user or user.is_bot or user.is_deleted:
                    continue

                # Skip whitelisted users immediately
                if is_whitelisted(group_id, user.id):
                    continue

                pfp_bytes = await _fetch_pfp(pyro, user.id)

                snapshot = UserSnapshot(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name or "",
                    last_name=user.last_name,
                    pfp_bytes=pfp_bytes,
                )

                result = await check_user(snapshot, group_id)
                checked += 1

                if result.flagged:
                    flagged += 1

                    async def _ban(gid: int, uid: int):
                        await bot.ban_chat_member(chat_id=gid, user_id=uid)

                    async def _notify(gid: int, text: str):
                        await bot.send_message(chat_id=gid, text=text, parse_mode="HTML")

                    log_notify = None
                    if log_channel_id:
                        async def log_notify(text: str, _lcid=log_channel_id):
                            await bot.send_message(chat_id=_lcid, text=text, parse_mode="HTML")

                    await ban_and_log(
                        result=result,
                        snapshot=snapshot,
                        group_id=group_id,
                        trigger="sweep",
                        ban_func=_ban,
                        notify_func=_notify,
                        log_channel_notify=log_notify,
                    )
                else:
                    mark_seen(group_id, user.id)

                if progress_cb and checked % 50 == 0:
                    await progress_cb(checked, flagged)

                # Respect Telegram rate limits between members
                await asyncio.sleep(0.05)

        except FloodWait as e:
            logger.warning(f"Sweep flood wait {e.value}s for group {group_id}")
            await asyncio.sleep(e.value)
        except (ChatAdminRequired, UserNotParticipant) as e:
            logger.error(f"Sweep permission error for group {group_id}: {e}")
            errors += 1
        except Exception as e:
            logger.error(f"Sweep error for group {group_id}: {e}")
            errors += 1

        # Refresh stored PFP hashes for all whitelisted users after each sweep
        await refresh_whitelist_pfps(pyro, group_id)

        return {"checked": checked, "flagged": flagged, "errors": errors}


async def refresh_whitelist_pfps(pyro: Client, group_id: int):
    """
    Re-download and re-hash the current profile photo for every whitelisted user.
    Called automatically after each sweep so stored hashes never go stale.
    """
    whitelist = get_whitelist(group_id)
    refreshed = 0
    for row in whitelist:
        pfp_bytes = await _fetch_pfp(pyro, row["user_id"])
        if not pfp_bytes:
            continue
        new_hash = compute_pfp_hash_bytes(pfp_bytes)
        if new_hash and new_hash != row["pfp_hash"]:
            upsert_whitelisted_user(
                group_id=group_id,
                user_id=row["user_id"],
                username=row["username"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                pfp_hash=new_hash,
                whitelisted_by=row["whitelisted_by"],
                user_type=row.get("user_type", "manual"),
            )
            refreshed += 1
        await asyncio.sleep(0.05)
    if refreshed:
        logger.info(f"Refreshed {refreshed} PFP hash(es) for group {group_id}.")


async def run_periodic_sweeps(pyro: Client, bot: Bot, log_channel_id: Optional[str] = None):
    """Background task: sweeps all registered groups every SWEEP_INTERVAL_HOURS hours."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_HOURS * 3600)
        group_ids = get_all_group_ids()
        logger.info(f"Starting periodic sweep of {len(group_ids)} group(s).")
        for gid in group_ids:
            result = await sweep_group(pyro, bot, gid, log_channel_id)
            logger.info(f"Sweep complete for {gid}: {result}")


async def _fetch_pfp(pyro: Client, user_id: int) -> Optional[bytes]:
    try:
        photos = pyro.get_chat_photos(user_id, limit=1)
        photo = await photos.__anext__()
        buf = BytesIO()
        async for chunk in pyro.stream_media(photo):
            buf.write(chunk)
        return buf.getvalue() or None
    except StopAsyncIteration:
        return None
    except Exception:
        return None
