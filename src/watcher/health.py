
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
from pyrogram.errors import FloodWait, RPCError, Unauthorized
from telegram import Bot

from src.config import HEALTH_CHECK_INTERVAL

logger = logging.getLogger(__name__)


async def _notify(bot: Bot, log_channel_id: Optional[str], text: str) -> None:
    if not log_channel_id:
        return
    try:
        await bot.send_message(chat_id=log_channel_id, text=text, parse_mode="HTML")
    except Exception:
        pass


async def run_health_check(pyro: Client, bot: Bot, log_channel_id: Optional[str] = None):
    """
    Periodic Pyrogram session liveness check.

    Probes with get_me() rather than the transport-level `is_connected` flag:
    a revoked/expired session can keep the socket up while every RPC fails 401,
    so is_connected would report "fine" on a dead watcher. get_me() actually
    exercises the auth key.

    - Unauthorized (revoked/expired session) is TERMINAL: alert once with
      regeneration instructions and stop probing — retrying can't fix it, and
      calling pyro.start() on an already-initialized client just raises.
    - Network/RPC blips are left to Pyrogram's own auto-reconnect; we only
      surface a warning and recovery notice.

    Loop body wrapped in try/except so a transient error can't kill the
    watchdog. CancelledError still propagates so shutdown works.
    """
    unhealthy = False   # currently in a degraded (non-auth) state
    session_dead = False  # terminal: session revoked, stop probing

    while True:
        try:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)

            if session_dead:
                continue  # nothing we can do until the operator regenerates the session

            try:
                await asyncio.wait_for(pyro.get_me(), timeout=30)
            except Unauthorized as e:
                session_dead = True
                logger.error(f"Pyrogram session is no longer authorized: {e}")
                await _notify(
                    bot, log_channel_id,
                    "❌ <b>Pyrogram session revoked/expired.</b> Profile-change "
                    "monitoring and sweeps are DOWN. Regenerate <code>PYROGRAM_SESSION</code> "
                    "and redeploy to restore them.",
                )
                continue
            except FloodWait as e:
                logger.warning(f"Health-check get_me() flood wait {e.value}s.")
                await asyncio.sleep(min(e.value, 300))
                continue
            except (asyncio.TimeoutError, RPCError, OSError, ConnectionError) as e:
                if not unhealthy:
                    unhealthy = True
                    logger.warning(f"Pyrogram health probe failed: {e} (auto-reconnect pending).")
                    await _notify(
                        bot, log_channel_id,
                        "⚠️ <b>Pyrogram watcher unhealthy.</b> Waiting for auto-reconnect…",
                    )
                continue

            # get_me() succeeded → healthy
            if unhealthy:
                unhealthy = False
                logger.info("Pyrogram watcher healthy again.")
                await _notify(bot, log_channel_id, "✅ <b>Pyrogram watcher healthy again.</b>")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Pyrogram health-check loop body crashed: {e}")
            await asyncio.sleep(30)
