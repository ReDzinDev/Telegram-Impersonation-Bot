
"""
Pyrogram connection health check.

Runs every HEALTH_CHECK_INTERVAL seconds. If the client has dropped its
connection, it attempts a reconnect and notifies the log channel.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pyrogram import Client
from telegram import Bot

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 300  # 5 minutes


async def run_health_check(pyro: Client, bot: Bot, log_channel_id: Optional[str] = None):
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)

        if pyro.is_connected:
            continue

        logger.warning("Pyrogram client disconnected — attempting reconnect.")

        if log_channel_id:
            try:
                await bot.send_message(
                    chat_id=log_channel_id,
                    text="⚠️ <b>Pyrogram watcher disconnected.</b> Attempting to reconnect…",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        try:
            await pyro.start()
            logger.info("Pyrogram client reconnected.")
            if log_channel_id:
                await bot.send_message(
                    chat_id=log_channel_id,
                    text="✅ <b>Pyrogram watcher reconnected.</b>",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"Pyrogram reconnect failed: {e}")
            if log_channel_id:
                try:
                    await bot.send_message(
                        chat_id=log_channel_id,
                        text=f"❌ <b>Pyrogram reconnect failed:</b> <code>{e}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
