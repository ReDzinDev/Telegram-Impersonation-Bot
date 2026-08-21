
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
    DatabaseUnavailable, run_db, get_group_sweep_offset, set_group_sweep_offset,
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
        partial  = False  # True if the sweep stopped before covering all members
        bios_skipped = 0  # members whose bio could NOT be keyword-screened (rate limit)

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
        has_keywords = bool(await run_db(get_reserved_keywords, group_id))
        from src.watcher.fetch import bio_cooldown_remaining, fetch_bio as _fetch_bio

        # Notify immediately so the admin knows the loop has started
        if progress_cb:
            await progress_cb(iterated, checked, flagged)

        # Where the last capped run stopped. Participant ordering is stable, so
        # without this the same prefix was re-scanned every run and the tail was
        # never reached — while /sweep told the admin "re-run to continue".
        #
        # We skip client-side rather than seeking server-side: get_chat_members
        # manages its own internal offset and exposes no parameter for it, and
        # driving raw channels.GetParticipants would be a far bigger, more
        # layer-sensitive change. Skipping still costs a few cheap enumeration
        # requests (200 members each), but the budget is spent almost entirely on
        # the paced per-member GetFullUser/photo calls, so the run now advances
        # into genuinely unscanned members instead of redoing the prefix.
        start_offset = await run_db(get_group_sweep_offset, group_id)
        if start_offset:
            logger.info(
                f"Resuming sweep of {group_id} after member {start_offset} "
                "(previous run hit the cap)."
            )
        position = 0        # members seen from the iterator, including skipped

        try:
            async for member in pyro.get_chat_members(group_id):
                position += 1
                if position <= start_offset:
                    continue          # already covered by an earlier run

                if time.monotonic() > sweep_deadline:
                    partial = True
                    logger.warning(
                        f"Sweep hard-cap reached for group {group_id}; stopping early "
                        f"after {iterated} members scanned this run (position "
                        f"{position - 1} overall) — the remainder will be picked up "
                        "next run."
                    )
                    break

                # Per-member isolation. Without this, ANY exception from
                # check_user, a hash, a fetch or a write fell through to the
                # generic handler below and terminated the whole group's sweep —
                # after three members, say — and it was then reported as a clean
                # run. One pathological avatar must cost one member, not the rest
                # of the group. (imagehash.phash is called outside image.py's own
                # try block, so this is a real path, not a hypothetical.)
                try:
                    iterated += 1
                    user = member.user
                    if not user or user.is_deleted:
                        continue

                    # Skip whitelisted users immediately
                    if await run_db(is_whitelisted, group_id, user.id):
                        continue

                    # Auto-whitelist current admins that /import_admins may have missed.
                    # Include admin bots (Rose, Combot, etc.) but skip the bot itself.
                    if member.status in (PyroChatMemberStatus.ADMINISTRATOR, PyroChatMemberStatus.OWNER):
                        if user.id == bot.id:
                            continue
                        # Bots don't usually have meaningful PFPs; skip the CDN download for them
                        pfp_bytes_admin = None if user.is_bot else await _fetch_pfp(pyro, user.id, wait=True)
                        await run_db(
                            upsert_whitelisted_user,
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
                        await run_db(mark_seen, group_id, user.id)
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
                        pfp_bytes = await _fetch_pfp(pyro, user.id, wait=True)
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
                    # wait=True rides out flood cooldowns instead of silently
                    # skipping; the pacer in src.watcher.fetch does the throttling.
                    if not result.flagged and has_keywords:
                        bio = await _fetch_bio(pyro, user.id, wait=True)
                        if bio is None and bio_cooldown_remaining() > 0:
                            # The fetch was skipped (or itself flooded) — this
                            # member's bio was NOT screened. Count it so the
                            # summary stays honest instead of overstating coverage.
                            bios_skipped += 1
                        if bio:
                            snapshot.bio = bio
                            result = await check_user(snapshot, group_id)

                    checked += 1

                    if result.flagged:
                        flagged += 1

                        # Per-group log channel, same as every foreground path.
                        # The summary below already resolved it correctly; the
                        # detections themselves did not.
                        from src.utils.checker import make_action_funcs, resolve_log_channel
                        channel = resolve_log_channel(group_id, log_channel_id)
                        ban_func, unban_func, log_notify = make_action_funcs(bot, channel)

                        await ban_and_log(
                            result=result,
                            snapshot=snapshot,
                            group_id=group_id,
                            trigger="sweep",
                            ban_func=ban_func,
                            unban_func=unban_func,
                            log_channel_notify=log_notify,
                        )
                    else:
                        await run_db(mark_seen, group_id, user.id)

                    # Progress update every 50 members iterated (not just checked)
                    # so the admin sees movement even when everyone is whitelisted/admin.
                    if progress_cb and iterated % 50 == 0:
                        await progress_cb(iterated, checked, flagged)

                    # Yield control to the event loop so concurrent PTB handlers
                    # (e.g. commands run during a sweep) can process their HTTP
                    # responses without timing out. Network-call pacing happens
                    # inside src.watcher.fetch, shared with every other caller.
                    await asyncio.sleep(0)
                except Exception as e:
                    errors += 1
                    logger.warning(
                        f"Skipping member {getattr(member.user, 'id', '?')} in "
                        f"{group_id} after an error: {e}"
                    )
                    continue

        except FloodWait as e:
            # The member enumeration itself got rate-limited; we can't cheaply
            # resume the async generator mid-stream, so this run is partial.
            # Sleep, mark partial, and DON'T immediately refresh PFPs (that would
            # fire a fresh media-download burst at the same flooded DC).
            partial = True
            logger.warning(
                f"Sweep flood wait {e.value}s for group {group_id} — ending run as partial."
            )
            # Tell the fetch pacers too: this is an account-wide limit, and they
            # would otherwise keep calling into the same flooded DC.
            from src.watcher.fetch import report_flood
            report_flood(e.value)
            # Cap the sleep. get_chat_members floods on a limited account run to
            # tens of minutes, and we hold the group's sweep lock throughout —
            # blocking /sweep and stalling the remaining groups.
            await asyncio.sleep(min(e.value, 300))
            await run_db(set_group_sweep_offset, group_id, position)
            result = {"iterated": iterated, "checked": checked, "flagged": flagged,
                      "errors": errors, "partial": True, "bios_skipped": bios_skipped}
            await run_db(record_sweep_run, group_id, iterated, checked, flagged, errors, trigger)
            return result
        except (ChatAdminRequired, UserNotParticipant) as e:
            logger.error(f"Sweep permission error for group {group_id}: {e}")
            errors += 1
            partial = True
        except Exception as e:
            # An exception mid-iteration always means incomplete coverage. This
            # used to leave partial=False, so the run was reported and recorded
            # as a clean sweep having covered only part of the group.
            logger.error(f"Sweep error for group {group_id}: {e}", exc_info=e)
            errors += 1
            partial = True

        # Persist (or clear) the resume point. A completed pass resets to 0 so
        # the next run starts from the top again.
        await run_db(set_group_sweep_offset, group_id, position if partial else 0)

        # Refresh stored PFP hashes for whitelisted users — but not when the run
        # was already cut short. This is unbounded work outside the deadline, and
        # the two cases where we get here partial are exactly the ones where the
        # budget is spent (the FloodWait path already returns early for the same
        # reason).
        if partial:
            logger.info(
                f"Skipping whitelist PFP refresh for {group_id}: run was partial."
            )
        else:
            await refresh_whitelist_pfps(pyro, group_id)

        result = {"iterated": iterated, "checked": checked, "flagged": flagged,
                  "errors": errors, "partial": partial, "bios_skipped": bios_skipped}
        # Persist this run so /stats and the daily summary can count it
        await run_db(record_sweep_run, group_id, iterated, checked, flagged, errors, trigger)
        return result


async def refresh_whitelist_pfps(pyro: Client, group_id: int):
    """
    Re-download and re-hash the current profile photo for every whitelisted user.
    Called automatically after each sweep so stored hashes never go stale.
    """
    whitelist = await run_db(get_whitelist, group_id)
    refreshed = 0
    for row in whitelist:
        # Pacing happens inside fetch; wait=True rides out flood cooldowns.
        pfp_bytes = await _fetch_pfp(pyro, row["user_id"], wait=True)
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
        await asyncio.sleep(0)
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
            all_ids = await run_db(get_all_group_ids)
            # Only sweep groups that have at least one whitelisted user — others
            # have nothing to check against.
            #
            # Per-group try: get_whitelist raises DatabaseUnavailable when a
            # group's protection state can't be established, and a comprehension
            # would let one such group abort the entire cycle. Skip that group
            # instead — sweeping it with an unknown whitelist is exactly the
            # fail-open behaviour we removed.
            group_ids = []
            for gid in all_ids:
                try:
                    if await run_db(get_whitelist, gid):
                        group_ids.append(gid)
                except DatabaseUnavailable as e:
                    logger.warning(f"Skipping sweep of {gid}: {e}")
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
    group = await run_db(get_group, group_id)
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
    if result.get("partial"):
        text += "\n⚠️ Partial — stopped early (rate limit or time cap)."
    if result.get("bios_skipped"):
        text += (
            f"\n⚠️ Bio checks skipped (rate limit): "
            f"<code>{result['bios_skipped']}</code>"
        )
    from src.utils.notify import send_log_message
    await send_log_message(bot, channel, text)


# PFP / bio fetch helpers live in src.watcher.fetch (shared, deduplicated).
# Aliased to the historical private names used throughout this module.
from src.watcher.fetch import fetch_pfp_bytes as _fetch_pfp  # noqa: E402
