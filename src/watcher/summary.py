
"""
Daily digest posted to the log channel once per day at midnight UTC.
Gives admins a passive overview without them needing to run /stats.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import Bot

from src.db import get_all_group_ids, get_stats

logger = logging.getLogger(__name__)


async def run_daily_summary(bot: Bot, log_channel_id: int):
    while True:
        now = datetime.now(timezone.utc)
        tomorrow_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        await asyncio.sleep((tomorrow_midnight - now).total_seconds())

        group_ids = get_all_group_ids()
        if not group_ids:
            continue

        lines = [f"📋 <b>Daily Summary</b> — {now.strftime('%Y-%m-%d UTC')}\n"]
        for gid in group_ids:
            s = get_stats(gid)
            lines.append(
                f"• <code>{gid}</code>: "
                f"{s.get('whitelisted', 0)} protected · "
                f"{s.get('detections', 0)} detections · "
                f"{s.get('banned', 0)} bans"
            )

        try:
            await bot.send_message(
                chat_id=log_channel_id,
                text="\n".join(lines),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send daily summary: {e}")
