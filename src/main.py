
import asyncio
import logging

from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, Update
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler, ChatMemberHandler,
    ContextTypes, MessageHandler, PicklePersistence, filters,
)

from src.config import (
    BOT_TOKEN, LOG_CHANNEL_ID,
    PYROGRAM_API_ID, PYROGRAM_API_HASH, PYROGRAM_SESSION, PYROGRAM_ENABLED,
)
from src.db import init_db, get_connection
from src.handlers.commands import (
    start, handle_chat_shared, import_admins, whitelist_user,
    unwhitelist_user, ban_user, unban_user,
    sweep, setaction, set_log_channel, list_whitelist, stats,
    handle_detection_callback,
    add_keyword, remove_keyword, list_keywords, set_threshold, logs, import_whitelist,
    clear_whitelist_cmd,
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


async def _db_keepalive(interval: int = 270) -> None:
    """
    Ping the database every *interval* seconds so Railway's Hobby Postgres
    never enters sleep mode between sweeps / activity bursts.

    Uses 270 s (just under 5 min) to stay inside psycopg's implicit
    idle-connection timeout and Railway's own inactivity window.
    On failure we log a warning and keep retrying — get_connection() will
    do its own exponential-backoff retry before giving up.
    """
    while True:
        await asyncio.sleep(interval)
        conn = get_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                logger.debug("DB keep-alive ping OK")
            except Exception as e:
                logger.warning(f"DB keep-alive query failed: {e}")
            finally:
                conn.close()
        else:
            logger.warning("DB keep-alive: could not connect (database may be waking up)")


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global PTB error handler.

    Network timeouts and transient connection errors are logged at WARNING level
    (they're expected during heavy sweeps and resolve on the next poll cycle).
    Everything else is logged at ERROR so real problems are still visible.
    """
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"Transient network error (ignored): {err}")
        return
    logger.error(f"Unhandled PTB exception", exc_info=err)


def build_ptb_app(pyro_client=None):
    persistence = PicklePersistence(filepath="bot_persistence")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .persistence(persistence)
        .build()
    )

    app.bot_data["log_channel_id"] = LOG_CHANNEL_ID

    # Commands
    app.add_handler(CommandHandler("start",           start))
    app.add_handler(CommandHandler("import_admins",   import_admins))
    app.add_handler(CommandHandler("whitelist",       whitelist_user))
    app.add_handler(CommandHandler("unwhitelist",     unwhitelist_user))
    app.add_handler(CommandHandler("ban",             ban_user))
    app.add_handler(CommandHandler("unban",           unban_user))
    app.add_handler(CommandHandler("sweep",           sweep))
    app.add_handler(CommandHandler("setaction",       setaction))
    app.add_handler(CommandHandler("setlogchannel",   set_log_channel))
    app.add_handler(CommandHandler("listwhitelist",   list_whitelist))
    app.add_handler(CommandHandler("stats",           stats))
    app.add_handler(CommandHandler("addkeyword",      add_keyword))
    app.add_handler(CommandHandler("removekeyword",   remove_keyword))
    app.add_handler(CommandHandler("listkeywords",    list_keywords))
    app.add_handler(CommandHandler("setthreshold",    set_threshold))
    app.add_handler(CommandHandler("logs",            logs))
    app.add_handler(CommandHandler("clearwhitelist",  clear_whitelist_cmd))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("csv") & filters.ChatType.PRIVATE,
        import_whitelist,
    ))

    # Inline keyboard callbacks from log-channel detection alerts
    app.add_handler(CallbackQueryHandler(
        handle_detection_callback,
        pattern=r"^(unban_wl|unban_fp|dismiss|ban_now|kick_now)\|",
    ))

    # Global error handler: keeps TimedOut / NetworkError out of the ERROR log
    app.add_error_handler(_error_handler)

    # Private group-picker flow
    app.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, handle_chat_shared))

    # Bot added to / removed from a group (auto-registers the group)
    app.add_handler(ChatMemberHandler(on_bot_added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))

    # New member joins
    app.add_handler(ChatMemberHandler(check_impersonation, ChatMemberHandler.CHAT_MEMBER))

    # First-message impersonation scan (relaxed — one check per user per group)
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
    # Set pyro_client AFTER initialize() so PicklePersistence doesn't overwrite it
    # (Pyrogram Client is not picklable — persisted bot_data would store None)
    if pyro_client:
        ptb_app.bot_data["pyro_client"] = pyro_client
    commands = [
        BotCommand("import_admins",   "Whitelist all current group admins"),
        BotCommand("whitelist",       "Whitelist a user (reply or ID)"),
        BotCommand("unwhitelist",     "Remove from whitelist (reply or ID)"),
        BotCommand("listwhitelist",   "Show whitelist + download CSV"),
        BotCommand("ban",             "Manually ban a user (reply or ID)"),
        BotCommand("unban",           "Unban a user by ID"),
        BotCommand("sweep",           "Run a full member scan"),
        BotCommand("setaction",       "Set detection action: ban, kick, or alert"),
        BotCommand("setlogchannel",   "Set per-group log channel"),
        BotCommand("stats",           "Show stats: all-time / 30d / 7d"),
        BotCommand("addkeyword",      "Add keyword(s) — supports *wildcards*, commas, r:regex"),
        BotCommand("removekeyword",   "Remove a reserved keyword"),
        BotCommand("listkeywords",    "List all reserved keywords"),
        BotCommand("setthreshold",    "Set fuzzy-match sensitivity (default 85)"),
        BotCommand("logs",            "Recent detections + admin actions"),
        BotCommand("clearwhitelist",  "⚠️ Remove all protected users (requires confirm)"),
    ]
    # Register commands for both private chats and groups
    await ptb_app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await ptb_app.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
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

        # Warm up entity cache — without this, get_chat_members fails with
        # PEER_ID_INVALID for groups the session has never interacted with.
        logger.info("Warming up Pyrogram entity cache (iterating dialogs)…")
        try:
            async for _ in pyro_client.get_dialogs():
                pass
            logger.info("Entity cache ready.")
        except Exception as e:
            logger.warning(f"Could not warm up entity cache: {e}")

        sweep_task = asyncio.create_task(
            run_periodic_sweeps(pyro_client, ptb_app.bot, LOG_CHANNEL_ID)
        )
        health_task = asyncio.create_task(
            run_health_check(pyro_client, ptb_app.bot, LOG_CHANNEL_ID)
        )

    # DB keep-alive — prevents Railway Hobby Postgres from sleeping
    keepalive_task = asyncio.create_task(_db_keepalive())

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
        keepalive_task.cancel()
        if summary_task:
            summary_task.cancel()
        if pyro_client:
            sweep_task.cancel()
            health_task.cancel()
            await pyro_client.stop()
        await ptb_app.updater.stop()
        await ptb_app.stop()
        await ptb_app.shutdown()
