
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
import time
from io import BytesIO
from typing import Optional

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus as PyroChatMemberStatus
from pyrogram.errors import FloodWait, ChatAdminRequired, UserNotParticipant
from telegram import Bot

from src.config import SWEEP_INTERVAL_HOURS, SWEEP_HARD_CAP_SECONDS
from src.db import (
    get_all_group_ids, get_group, get_reserved_keywords, get_whitelist,
    is_whitelisted, mark_seen, record_sweep_run, upsert_whitelisted_user,
)
from src.utils.checker import UserSnapshot, check_user, ban_and_log
from src.utils.image import compute_pfp_hash_bytes

logger = logging.getLogger(__name__)


_sweep_locks: dict[int, asyncio.Lock] = {}


async def sweep_group(
    pyro: Client,
    bot: Bot,
    group_id: int,
    log_channel_id: Optional[str] = None,
    progress_cb=None,
    trigger: str = "manual",
) -> dict:
    """
    Sweep all members of group_id.

    progress_cb(iterated, checked, flagged) — optional live-update callback.
    trigger                                 — "manual" or "auto"; recorded in
                                              sweep_runs so we can show
                                              "sweeps in the last 24h / 30d".

    Returns a summary dict with keys: iterated, checked, flagged, errors.
    """
    if group_id not in _sweep_locks:
        _sweep_locks[group_id] = asyncio.Lock()

    if _sweep_locks[group_id].locked():
        return {"status": "already_running"}

    async with _sweep_locks[group_id]:
        checked  = 0   # members actually run through the detection pipeline
        flagged  = 0
        errors   = 0
        iterated = 0   # every member the loop touches (including admins, bots, whitelisted)

        try:
            # Resolve the peer first — required for new sessions where the entity
            # isn't yet in Pyrogram's local cache.
            # Timeout prevents a Pyrogram network hang from holding the lock forever.
            await asyncio.wait_for(pyro.get_chat(group_id), timeout=30)
        except asyncio.TimeoutError:
            logger.error(f"Timeout resolving group {group_id} for sweep (>30s) — releasing lock.")
            return {"iterated": 0, "checked": 0, "flagged": 0, "errors": 1}
        except Exception as e:
            logger.error(f"Cannot resolve group {group_id} for sweep: {e}")
            return {"iterated": 0, "checked": 0, "flagged": 0, "errors": 1}

        sweep_deadline = time.monotonic() + SWEEP_HARD_CAP_SECONDS  # hard cap per group

        # Bios are expensive (one MTProto GetFullUser call each) and irrelevant
        # for groups with no reserved keywords — bio is only consulted by the
        # keyword detection stage. Resolve once and skip the call otherwise.
        has_keywords = bool(get_reserved_keywords(group_id))
        from src.watcher.events import _fetch_bio   # local import — avoids module cycle

        # Notify immediately so the admin knows the loop has started
        if progress_cb:
            await progress_cb(iterated, checked, flagged)

        try:
            async for member in pyro.get_chat_members(group_id):
                if time.monotonic() > sweep_deadline:
                    logger.warning(
                        f"Sweep hard-cap (2 h) reached for group {group_id}; "
                        f"stopping early after {iterated} members iterated."
                    )
                    break

                iterated += 1
                user = member.user
                if not user or user.is_deleted:
                    continue

                # Skip whitelisted users immediately
                if is_whitelisted(group_id, user.id):
                    continue

                # Auto-whitelist current admins that /import_admins may have missed.
                # Include admin bots (Rose, Combot, etc.) but skip the bot itself.
                if member.status in (PyroChatMemberStatus.ADMINISTRATOR, PyroChatMemberStatus.OWNER):
                    if user.id == bot.id:
                        continue
                    # Bots don't usually have meaningful PFPs; skip the CDN download for them
                    pfp_bytes_admin = None if user.is_bot else await _fetch_pfp(pyro, user.id)
                    upsert_whitelisted_user(
                        group_id=group_id,
                        user_id=user.id,
                        username=user.username,
                        first_name=user.first_name or "",
                        last_name=user.last_name,
                        pfp_hash=compute_pfp_hash_bytes(pfp_bytes_admin) if pfp_bytes_admin else None,
                        whitelisted_by=bot.id,
                        user_type="admin",
                        is_bot=bool(user.is_bot),
                    )
                    mark_seen(group_id, user.id)
                    continue

                # Non-admin bots can't impersonate anyone — skip them
                if user.is_bot:
                    continue

                # Fast path: username + name checks only — no PFP download
                snapshot = UserSnapshot(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name or "",
                    last_name=user.last_name,
                    pfp_bytes=None,
                )

                result = await check_user(snapshot, group_id)

                # Lazy PFP: only fetch when there's a weak name match that needs confirmation
                if result.needs_pfp:
                    pfp_bytes = await _fetch_pfp(pyro, user.id)
                    if pfp_bytes:
                        snapshot = UserSnapshot(
                            user_id=user.id,
                            username=user.username,
                            first_name=user.first_name or "",
                            last_name=user.last_name,
                            pfp_bytes=pfp_bytes,
                        )
                        result = await check_user(snapshot, group_id)

                # Lazy bio: name/username were clean, but the group has reserved
                # keywords — a scammer's banned word might be hiding in their bio
                # (which Bot API can't see and `get_chat_members` doesn't return).
                # One extra MTProto call per still-unflagged non-bot member.
                bio_fetched = False
                if not result.flagged and has_keywords:
                    bio = await _fetch_bio(pyro, user.id)
                    bio_fetched = True
                    if bio:
                        snapshot.bio = bio
                        result = await check_user(snapshot, group_id)

                checked += 1

                if result.flagged:
                    flagged += 1

                    async def _ban(gid: int, uid: int):
                        await bot.ban_chat_member(chat_id=gid, user_id=uid)

                    log_notify = None
                    if log_channel_id:
                        from src.utils.notify import send_log_message
                        async def log_notify(text: str, markup=None, _lcid=log_channel_id):
                            await send_log_message(
                                bot, _lcid, text, reply_markup=markup, raise_on_error=True,
                            )

                    async def _unban(gid: int, uid: int):
                        await bot.unban_chat_member(chat_id=gid, user_id=uid)

                    await ban_and_log(
                        result=result,
                        snapshot=snapshot,
                        group_id=group_id,
                        trigger="sweep",
                        ban_func=_ban,
                        unban_func=_unban,
                        log_channel_notify=log_notify,
                    )
                else:
                    mark_seen(group_id, user.id)

                # Progress update every 50 members iterated (not just checked)
                # so the admin sees movement even when everyone is whitelisted/admin.
                if progress_cb and iterated % 50 == 0:
                    await progress_cb(iterated, checked, flagged)

                # Yield control to the event loop so concurrent PTB handlers
                # (e.g. commands run during a sweep) can process their HTTP
                # responses without timing out.
                await asyncio.sleep(0)

                # Only pace after an actual network call — username/name checks
                # are pure CPU and need no delay. Sleeping unconditionally was
                # the reason sweeps took 8+ minutes for 1,000-member groups.
                if result.needs_pfp and pfp_bytes:
                    await asyncio.sleep(0.5)  # back off after a CDN media fetch
                elif bio_fetched:
                    await asyncio.sleep(0.3)  # back off after a GetFullUser call

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

        result = {"iterated": iterated, "checked": checked, "flagged": flagged, "errors": errors}
        # Persist this run so /stats and the daily summary can count it
        record_sweep_run(group_id, iterated, checked, flagged, errors, trigger)
        return result


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
                is_bot=bool(row.get("is_bot", False)),
            )
            refreshed += 1
        await asyncio.sleep(0.05)
    if refreshed:
        logger.info(f"Refreshed {refreshed} PFP hash(es) for group {group_id}.")


async def run_periodic_sweeps(pyro: Client, bot: Bot, log_channel_id: Optional[str] = None):
    """
    Background task: sweeps all configured groups every SWEEP_INTERVAL_HOURS hours.
    The first sweep is delayed by a full interval — the bot does NOT sweep on startup.
    Admins should run /sweep manually after initial setup.

    After each sweep we post a short per-group summary to that group's
    configured log channel (falling back to the global LOG_CHANNEL_ID).

    The entire loop body is wrapped in try/except so a transient failure
    (DB down, network blip) just logs and waits for the next cycle —
    never kills the task. Per-group failures inside sweep_group already
    have their own handlers; this catches anything that escapes.
    """
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_HOURS * 3600)
            all_ids = get_all_group_ids()
            # Only sweep groups that have at least one whitelisted user — others
            # have nothing to check against
            group_ids = [gid for gid in all_ids if get_whitelist(gid)]
            logger.info(
                f"Starting scheduled sweep of {len(group_ids)}/{len(all_ids)} "
                "group(s) (skipping unconfigured)."
            )
            for gid in group_ids:
                try:
                    result = await sweep_group(pyro, bot, gid, log_channel_id, trigger="auto")
                    logger.info(f"Scheduled sweep complete for {gid}: {result}")
                    await _post_sweep_summary(bot, gid, result, log_channel_id)
                except Exception as e:
                    # Per-group failure: log and keep going for other groups
                    logger.exception(f"Periodic sweep failed for group {gid}: {e}")
        except asyncio.CancelledError:
            # Propagate cancellation so the task can exit cleanly on shutdown
            raise
        except Exception as e:
            # Outer-loop failure: log and let the while True re-enter after
            # a short delay so we don't tight-loop on a persistent error
            logger.exception(f"Periodic sweep loop body crashed: {e}")
            await asyncio.sleep(60)


async def _post_sweep_summary(
    bot: Bot, group_id: int, result: dict, fallback_channel_id: Optional[str]
) -> None:
    """
    Send a per-run summary of an auto-sweep to the group's log channel
    (or the global fallback channel). Silently no-ops if no channel is
    configured anywhere.
    """
    group = get_group(group_id)
    channel = (group and group.get("log_channel_id")) or fallback_channel_id
    if not channel:
        return

    title = (group and group.get("title")) or str(group_id)
    text = (
        f"🧹 <b>Auto-sweep complete</b>\n"
        f"<b>Group:</b> {title} (<code>{group_id}</code>)\n"
        f"Members seen: <code>{result.get('iterated', 0)}</code>\n"
        f"Checked: <code>{result.get('checked', 0)}</code>\n"
        f"Flagged: <code>{result.get('flagged', 0)}</code>\n"
        f"Errors: <code>{result.get('errors', 0)}</code>"
    )
    from src.utils.notify import send_log_message
    await send_log_message(bot, channel, text)


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
    except FloodWait as e:
        # DC-level rate limit on media downloads — skip this PFP rather than
        # blocking the entire sweep for potentially 20+ minutes.
        logger.warning(f"PFP flood wait {e.value}s for user {user_id} — skipping photo check.")
        return None
    except Exception:
        return None
