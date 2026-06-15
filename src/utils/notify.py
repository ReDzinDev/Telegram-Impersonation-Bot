
"""
Centralized log-channel sender with failure tracking.

Every "send to log channel" path in the bot funnels through send_log_message().
After N consecutive failures sending to the same channel — almost always
because the bot was kicked from that channel or the channel was deleted —
we post a notice to the global LOG_CHANNEL_ID naming every group that
relies on it, so the operator can intervene before more alerts vanish.

Why centralize this rather than rely on per-call try/except?
  - Auto-sweeps, message scans, profile-change events, and manual
    /ban|/unban|/sweep all post to log channels via different code paths
    (closures or direct send_message). Tracking failures per channel was
    impossible without funneling them through one chokepoint.
  - The previous behaviour was silent: a kicked-bot channel made every
    detection in that group disappear into a `logger.warning` line.
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Bot, InlineKeyboardMarkup

from src.config import LOG_CHANNEL_ID
from src.db import get_all_group_ids, get_group

logger = logging.getLogger(__name__)


# Per-channel consecutive-failure counter. Reset on every successful send.
_failures: dict[int, int] = {}
_alerted:  set[int]       = set()  # channels we've already warned about
_FAILURE_THRESHOLD = 3


async def send_log_message(
    bot: Bot,
    channel_id: int | str,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    *,
    raise_on_error: bool = False,
) -> bool:
    """
    Send `text` (HTML) to the given log channel. Returns True on success.

    Failures are tracked per-channel: after _FAILURE_THRESHOLD consecutive
    misses we post one warning to the global LOG_CHANNEL_ID naming the
    affected groups, then go quiet until a successful send resets the
    counter. Repeated alerts would themselves be spam.

    raise_on_error=True is for callers (like ban_and_log) that already
    have their own error logging — they get the exception, we still
    track the failure for the global tracker.
    """
    try:
        await bot.send_message(
            chat_id=channel_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        await _record_failure(bot, channel_id, e)
        if raise_on_error:
            raise
        return False

    _record_success(channel_id)
    return True


def _record_success(channel_id: int | str) -> None:
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return
    if _failures.pop(cid, None) is not None:
        logger.info(f"Log channel {cid} recovered after previous failures.")
    _alerted.discard(cid)


async def _record_failure(bot: Bot, channel_id: int | str, exc: Exception) -> None:
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        # Non-numeric channel id (shouldn't happen) — just log and bail
        logger.warning(f"send_log_message failed for {channel_id!r}: {exc}")
        return

    count = _failures.get(cid, 0) + 1
    _failures[cid] = count
    logger.warning(f"Log channel {cid} send failed ({count}x consecutive): {exc}")

    if count >= _FAILURE_THRESHOLD and cid not in _alerted:
        _alerted.add(cid)
        await _alert_operator(bot, cid, exc)


async def _alert_operator(bot: Bot, channel_id: int, last_exc: Exception) -> None:
    """
    Post one warning to the global LOG_CHANNEL_ID listing every group
    whose log channel is currently the broken one. Goes quiet after
    posting once per outage — _record_success() resets the gate.
    """
    if not LOG_CHANNEL_ID:
        return
    # Don't recursively alert about the global channel itself
    try:
        if int(LOG_CHANNEL_ID) == channel_id:
            return
    except (TypeError, ValueError):
        pass

    # Find groups using this channel (their per-group log_channel_id matches)
    affected: list[str] = []
    try:
        for gid in get_all_group_ids():
            grp = get_group(gid)
            if grp and grp.get("log_channel_id") == channel_id:
                title = grp.get("title") or str(gid)
                affected.append(f"• {title} (<code>{gid}</code>)")
    except Exception as e:
        logger.warning(f"Could not enumerate affected groups for {channel_id}: {e}")

    affected_block = (
        "\n".join(affected) if affected
        else "<i>(global fallback channel, no per-group bindings)</i>"
    )

    text = (
        f"📡 <b>Log channel unreachable</b>\n"
        f"Channel <code>{channel_id}</code> has failed "
        f"{_FAILURE_THRESHOLD} sends in a row.\n"
        f"<b>Last error:</b> <code>{type(last_exc).__name__}: {str(last_exc)[:200]}</code>\n\n"
        f"<b>Affected groups:</b>\n{affected_block}\n\n"
        "<i>Most common cause: the bot was kicked or lost admin rights in the channel. "
        "Re-add it as an admin or run /setlogchannel to point somewhere else.</i>"
    )
    try:
        await bot.send_message(
            chat_id=LOG_CHANNEL_ID, text=text, parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        # The global channel is broken too. Nothing left to do but log it.
        logger.error(f"Could not post log-channel-unreachable warning to global: {e}")
