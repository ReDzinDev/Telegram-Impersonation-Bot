
import asyncio
import logging

from telegram import BotCommand, Update
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler, ChatMemberHandler,
    MessageHandler, filters,
)

from src.config import (
    BOT_TOKEN, LOG_CHANNEL_ID,
    PYROGRAM_API_ID, PYROGRAM_API_HASH, PYROGRAM_SESSION, PYROGRAM_ENABLED,
)
from src.db import init_db
from src.handlers.commands import (
    start, handle_chat_shared, import_admins, whitelist_user,
    unwhitelist_user, check_user_cmd, ban_user, unban_user,
    sweep, setmode, setaction, set_log_channel, list_whitelist, stats,
    watch_user, handle_detection_callback, export_whitelist,
)
from src.handlers.member_join import check_impersonation, on_bot_added_to_group
from src.handlers.messages import scan_message_sender

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("pyrogram").setLevel(logging.WARNING)


def build_ptb_app(pyro_client=None):
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.bot_data["log_channel_id"] = LOG_CHANNEL_ID
    if pyro_client:
        app.bot_data["pyro_client"] = pyro_client

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("import_admins", import_admins))
    app.add_handler(CommandHandler("whitelist", whitelist_user))
    app.add_handler(CommandHandler("unwhitelist", unwhitelist_user))
    app.add_handler(CommandHandler("check", check_user_cmd))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("sweep", sweep))
    app.add_handler(CommandHandler("setmode", setmode))
    app.add_handler(CommandHandler("setaction", setaction))
    app.add_handler(CommandHandler("setlogchannel", set_log_channel))
    app.add_handler(CommandHandler("listwhitelist", list_whitelist))
    app.add_handler(CommandHandler("exportwhitelist", export_whitelist))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("watch", watch_user))

    # Inline keyboard callbacks from log-channel detection alerts
    app.add_handler(CallbackQueryHandler(
        handle_detection_callback, pattern=r"^(unban_wl|dismiss)\|"
    ))

    # Private group-picker flow
    app.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, handle_chat_shared))

    # Bot added to / removed from a group (auto-registers the group)
    app.add_handler(ChatMemberHandler(on_bot_added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))

    # New member joins
    app.add_handler(ChatMemberHandler(check_impersonation, ChatMemberHandler.CHAT_MEMBER))

    # Message scanning (STRICT / RELAXED)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        scan_message_sender,
    ))

    return app


async def main():
    init_db()

    pyro_client = None

    if PYROGRAM_ENABLED:
        from src.watcher.client import build_client
        from src.watcher.events import register_event_handlers
        from src.watcher.sweep import run_periodic_sweeps
        from src.watcher.health import run_health_check

        pyro_client = build_client(PYROGRAM_API_ID, PYROGRAM_API_HASH, PYROGRAM_SESSION)
        logger.info("Pyrogram watcher enabled.")
    else:
        logger.warning(
            "Pyrogram watcher is DISABLED. Set PYROGRAM_API_ID, PYROGRAM_API_HASH, "
            "and PYROGRAM_SESSION to enable profile-change monitoring and full sweeps."
        )

    ptb_app = build_ptb_app(pyro_client)

    # Wire up Pyrogram event handlers (needs the ptb bot reference)
    if pyro_client:
        register_event_handlers(pyro_client, ptb_app.bot, LOG_CHANNEL_ID)

    # Start PTB (non-blocking polling)
    await ptb_app.initialize()
    await ptb_app.bot.set_my_commands([
        BotCommand("import_admins",   "Whitelist all current group admins"),
        BotCommand("whitelist",       "Whitelist a user (reply)"),
        BotCommand("unwhitelist",     "Remove from whitelist (reply or ID)"),
        BotCommand("watch",           "Protect a non-admin VIP (reply or ID)"),
        BotCommand("listwhitelist",   "Show all protected users"),
        BotCommand("exportwhitelist", "Download whitelist as CSV"),
        BotCommand("check",           "Manually check a user (reply)"),
        BotCommand("ban",             "Manually ban a user (reply or ID)"),
        BotCommand("unban",           "Unban a user by ID"),
        BotCommand("sweep",           "Run a full member scan"),
        BotCommand("setmode",         "Set scan mode: strict or relaxed"),
        BotCommand("setaction",       "Set detection action: ban, kick, or alert"),
        BotCommand("setlogchannel",   "Set per-group log channel"),
        BotCommand("stats",           "Show detection and ban stats"),
    ])
    await ptb_app.start()
    await ptb_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    if LOG_CHANNEL_ID:
        try:
            pyro_status = "✅ Pyrogram watcher active" if PYROGRAM_ENABLED else "⚠️ Pyrogram watcher disabled"
            await ptb_app.bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=f"🟢 <b>Anti-Impersonator Bot started</b>\n{pyro_status}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Could not send startup message to log channel: {e}")

    logger.info("Bot is running.")

    if pyro_client:
        await pyro_client.start()
        logger.info("Pyrogram client started.")

        sweep_task = asyncio.create_task(
            run_periodic_sweeps(pyro_client, ptb_app.bot, LOG_CHANNEL_ID)
        )
        health_task = asyncio.create_task(
            run_health_check(pyro_client, ptb_app.bot, LOG_CHANNEL_ID)
        )

    summary_task = None
    if LOG_CHANNEL_ID:
        from src.watcher.summary import run_daily_summary
        summary_task = asyncio.create_task(
            run_daily_summary(ptb_app.bot, LOG_CHANNEL_ID)
        )

    try:
        await asyncio.Event().wait()  # run forever
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down…")
        if summary_task:
            summary_task.cancel()
        if pyro_client:
            sweep_task.cancel()
            health_task.cancel()
            await pyro_client.stop()
        await ptb_app.updater.stop()
        await ptb_app.stop()
        await ptb_app.shutdown()
