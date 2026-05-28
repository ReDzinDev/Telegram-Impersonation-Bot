
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

from src.db import get_all_group_ids, get_group, get_recent_activity

logger = logging.getLogger(__name__)


async def run_daily_summary(bot: Bot, log_channel_id: int):
    first_run = True
    while True:
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

        group_ids = get_all_group_ids()
        if not group_ids:
            continue

        # Build per-group rows AND aggregate totals so the digest is useful at a glance
        per_group_lines = []
        totals = {"detections": 0, "banned": 0, "kicked": 0, "alerted": 0, "sweeps": 0}

        for gid in group_ids:
            act = get_recent_activity(gid, hours=24)
            if not act:
                continue
            for k in totals:
                totals[k] += act.get(k, 0) or 0
            # Skip groups with zero activity to keep the digest tight
            if not any(act.get(k) for k in totals):
                continue

            group = get_group(gid)
            title = (group and group.get("title")) or str(gid)
            per_group_lines.append(
                f"• <b>{title}</b> — "
                f"🚨 {act.get('detections', 0)} · "
                f"🚫 {act.get('banned', 0)} · "
                f"👢 {act.get('kicked', 0)} · "
                f"🔕 {act.get('alerted', 0)} · "
                f"🧹 {act.get('sweeps', 0)}"
            )

        header = (
            f"📋 <b>Daily Summary — last 24h</b> "
            f"({now.strftime('%Y-%m-%d UTC')})\n\n"
            f"<b>Across all groups:</b> "
            f"🚨 {totals['detections']} detections · "
            f"🚫 {totals['banned']} bans · "
            f"👢 {totals['kicked']} kicks · "
            f"🔕 {totals['alerted']} alerts · "
            f"🧹 {totals['sweeps']} sweeps\n"
        )

        body = (
            "\n<b>By group:</b>\n" + "\n".join(per_group_lines)
            if per_group_lines
            else "\n<i>No activity in any group in the last 24h.</i>"
        )

        try:
            await bot.send_message(
                chat_id=log_channel_id,
                text=header + body,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send daily summary: {e}")
