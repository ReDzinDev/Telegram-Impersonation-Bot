
"""
Daily digest posted to the log channel once per day at midnight UTC.

Reports activity from the last 24 hours per group (detections, bans,
kicks, alerts, sweeps run) rather than cumulative all-time stats —
admins use /stats for the windowed breakdown when they want totals.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram import Bot

from src.db import get_all_group_ids, get_group, get_recent_activity, purge_old_records
from src.utils.notify import send_log_message

logger = logging.getLogger(__name__)


async def run_daily_summary(bot: Bot, log_channel_id: int):
    """
    Background task: posts a 24h activity digest at midnight UTC.

    The whole loop body is wrapped in try/except so a transient DB error
    or send failure doesn't permanently kill the task. A failure logs +
    waits a minute + restarts the next-midnight calculation.
    """
    first_run = True
    while True:
        try:
            now = datetime.now(timezone.utc)
            tomorrow_midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            wait_seconds = (tomorrow_midnight - now).total_seconds()

            # Startup grace: if we'd fire within an hour of booting, skip the
            # imminent midnight and wait for the next one. Otherwise a deploy
            # at 23:55 UTC posts a near-empty digest 5 minutes later.
            if first_run and wait_seconds < 3600:
                logger.info(
                    f"Skipping next midnight digest (only {wait_seconds/60:.0f} min away); "
                    "will post tomorrow instead."
                )
                wait_seconds += 24 * 3600
            first_run = False

            await asyncio.sleep(wait_seconds)

            # Nightly retention purge — piggybacks on the once-a-day cadence so
            # bounded-window tables don't grow forever on small Railway disks.
            try:
                purge_old_records()
            except Exception as e:
                logger.error(f"Retention purge failed: {e}")

            group_ids = get_all_group_ids()
            if not group_ids:
                continue

            date_str = now.strftime("%Y-%m-%d UTC")
            _KEYS = ("detections", "banned", "kicked", "alerted", "sweeps")

            for gid in group_ids:
                act = get_recent_activity(gid, hours=24)
                if not act:
                    continue

                # Skip groups with zero activity — no point posting a blank digest.
                if not any(act.get(k) for k in _KEYS):
                    continue

                group = get_group(gid)
                title = (group and group.get("title")) or str(gid)

                # Route to this group's own log channel; fall back to the global one.
                channel = (group and group.get("log_channel_id")) or log_channel_id
                if not channel:
                    logger.debug(f"No log channel for group {gid} — skipping daily summary.")
                    continue

                text = (
                    f"📋 <b>Daily Summary — {title}</b> ({date_str})\n"
                    f"<i>Last 24 hours only — use /stats for cumulative totals.</i>\n\n"
                    f"🚨 Detections: <code>{act.get('detections', 0)}</code>\n"
                    f"🚫 Banned: <code>{act.get('banned', 0)}</code>\n"
                    f"👢 Kicked: <code>{act.get('kicked', 0)}</code>\n"
                    f"🔕 Alerted: <code>{act.get('alerted', 0)}</code>\n"
                    f"🧹 Sweeps: <code>{act.get('sweeps', 0)}</code>"
                )

                try:
                    await send_log_message(bot, channel, text)
                except Exception as e:
                    logger.error(f"Failed to send daily summary for group {gid}: {e}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Catch-all so a transient bug doesn't permanently kill the daily digest.
            # Brief sleep prevents tight-looping if something is persistently broken.
            logger.exception(f"Daily summary loop body crashed: {e}")
            await asyncio.sleep(60)
