
import asyncio
import html
import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor

# Configure logging BEFORE importing anything under src, so the import-time
# messages src.config emits are formatted by our handler rather than by
# logging's last-resort stderr fallback.
from src.utils.logging_setup import setup_logging

setup_logging(quiet=(
    "httpx",
    "httpcore",
    "pyrogram",
    "telegram.ext.Updater",
    # psycopg_pool logs "connection requested"/"connection given" at INFO on
    # EVERY borrow. A 1,000-member sweep emitted several thousand lines of it —
    # real noise and real log-ingest cost.
    "psycopg.pool",
))

from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, Update
from telegram.error import TimedOut, NetworkError, Conflict
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler, ChatMemberHandler,
    ContextTypes, MessageHandler, PicklePersistence, filters,
)

from src.config import (
    BOT_TOKEN, LOG_CHANNEL_ID,
    PYROGRAM_API_ID, PYROGRAM_API_HASH, PYROGRAM_SESSION, PYROGRAM_ENABLED,
    BLOCKLIST_TRUSTED_GROUPS,
)
from src.db import (
    init_db, get_connection, put_connection, purge_old_records, run_db,
    DB_POOL_MAX_SIZE,
)
from src.handlers.commands import (
    start, handle_chat_shared, import_admins, whitelist_user,
    unwhitelist_user, ban_user, unban_user,
    sweep, setaction, set_log_channel, list_whitelist, stats,
    handle_detection_callback,
    add_keyword, remove_keyword, list_keywords, set_threshold, logs, import_whitelist,
    clear_whitelist_cmd,
    settings, set_bands, set_type_threshold, blocklist_toggle, protect_identity,
    handle_whitelist_undo, handle_whitelist_page, handle_logs_page,
)
from src.handlers.member_join import check_impersonation, on_bot_added_to_group
from src.handlers.messages import scan_message_sender

# Logging is configured by setup_logging() above, before the src imports.
# Railway derives severity from the STREAM, not the text: stdout is info,
# stderr is error. basicConfig's default handler writes to stderr, so every
# INFO line the bot emitted arrived in Railway's error bucket — red, and
# indistinguishable from a real failure. setup_logging puts a single handler on
# stdout and, on Railway, emits one JSON object per line with an explicit
# level, which is the only thing Railway trusts over the stream. That also
# makes @level:error and @logger:<module> filtering work in the log explorer.
logger = logging.getLogger(__name__)

# asyncio holds only a WEAK reference to a bare create_task result, so a
# fire-and-forget task can be garbage-collected before it ever runs. That is
# unacceptable for the "a background task died" notification specifically: it is
# the one operator-visible signal that protection has silently stopped. Tasks
# live here until they finish. (ruff RUF006 flags exactly this pattern.)
_background_notifications: set[asyncio.Task] = set()


async def _db_keepalive(interval: int = 270) -> None:
    """
    Ping the database every *interval* seconds so Railway's Hobby Postgres
    never enters sleep mode between sweeps / activity bursts.

    Uses 270 s (just under 5 min) to stay inside psycopg's implicit
    idle-connection timeout and Railway's own inactivity window.
    On failure we log a warning and keep retrying — get_connection() will
    do its own exponential-backoff retry before giving up. The whole
    body is wrapped in try/except so nothing here can kill the task.
    """
    while True:
        try:
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
                    put_connection(conn)
            else:
                logger.warning("DB keep-alive: could not connect (database may be waking up)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"DB keep-alive loop body crashed: {e}")
            await asyncio.sleep(30)


# Restart pacing for a background task that dies. Starts gentle so a transient
# fault recovers quickly, backs off so a persistently broken task does not spin,
# and resets after a healthy stretch so a task that ran for hours before failing
# is not punished for its predecessor.
_SUPERVISOR_BASE_DELAY = 30.0      # seconds before the first restart
_SUPERVISOR_MAX_DELAY = 600.0      # ceiling on the backoff
_SUPERVISOR_HEALTHY_AFTER = 300.0  # a run this long resets the backoff


async def _supervised(name: str, factory, notify=None) -> None:
    """
    Run `factory()` forever, restarting it when it dies.

    The previous supervisor attached a done-callback that logged at ERROR and
    posted to the log channel — useful, but it never restarted anything. If
    run_periodic_sweeps died, scheduled sweeps stopped FOREVER while the bot
    carried on serving commands as though healthy, and recovery depended on a
    human noticing one Telegram message.

    `factory` is a zero-argument callable returning a fresh coroutine, not a
    coroutine: a coroutine can only be awaited once, so restarting requires the
    ability to build a new one.

    Returning cleanly counts as dying. Every task here is a `while True` loop, so
    a normal return means something is wrong, not that the work is finished.

    CancelledError propagates untouched — shutdown must not have to fight the
    restart loop.
    """
    delay = _SUPERVISOR_BASE_DELAY
    while True:
        started = time.monotonic()
        exc: BaseException | None = None
        try:
            await factory()
            reason = "returned cleanly, but its loop should never exit"
        except asyncio.CancelledError:
            logger.info(f"Background task '{name}' cancelled (shutdown).")
            raise
        except Exception as e:                     # noqa: BLE001 - last resort
            exc = e
            reason = f"{type(e).__name__}: {e}"

        ran_for = time.monotonic() - started
        if ran_for >= _SUPERVISOR_HEALTHY_AFTER:
            # It was healthy for a long time; treat this as a fresh incident.
            delay = _SUPERVISOR_BASE_DELAY

        logger.error(
            f"Background task '{name}' died after {ran_for:.0f}s ({reason}); "
            f"restarting in {delay:.0f}s.",
            exc_info=exc,
            extra={"task": name, "ran_for_s": round(ran_for), "restart_in_s": delay},
        )
        if notify is not None:
            try:
                await notify(name, exc)
            except Exception:
                logger.warning(f"Could not report death of '{name}' to the operator.")

        await asyncio.sleep(delay)
        delay = min(delay * 2, _SUPERVISOR_MAX_DELAY)


async def _loop_lag_watchdog(threshold: float = 5.0, interval: float = 1.0) -> None:
    """
    Report an event loop that is alive but not running.

    This is the only cheap detector for a blocking call finding its way back into
    an async path. A process frozen inside time.sleep — or inside a catastrophic
    regex, since CPython's `re` holds the GIL — looks perfectly healthy from the
    outside: the container is up, the process is running, nothing has crashed. It
    simply stops answering Telegram.

    Sleeping for `interval` and measuring how much longer it actually took is
    enough to see it.
    """
    loop = asyncio.get_running_loop()
    while True:
        before = loop.time()
        await asyncio.sleep(interval)
        lag = loop.time() - before - interval
        if lag > threshold:
            logger.error(
                f"Event loop stalled for {lag:.1f}s (threshold {threshold:.0f}s). "
                "Something blocking is running on the loop — check for a db call "
                "outside run_db, or a slow regex.",
                extra={"lag_s": round(lag, 2)},
            )


async def _retention_loop(interval_hours: int = 24) -> None:
    """
    Delete rows past their retention window, forever, on its own schedule.

    This used to live inside run_daily_summary — and main() only creates that
    task `if LOG_CHANNEL_ID`. So on a deployment where every group sets its own
    /setlogchannel and no global channel exists, retention NEVER RAN and logs,
    sweep_runs, name_change_log, false_positives, seen_members and admin_actions
    grew without bound on a Hobby-tier disk. Housekeeping the database must not
    depend on whether Telegram notifications are configured.

    Runs off the event loop, since the DELETEs are blocking psycopg. The first
    pass is delayed by one interval so it never competes with startup.
    """
    while True:
        try:
            await asyncio.sleep(interval_hours * 3600)
            deleted = await run_db(purge_old_records)
            if any(deleted.values()):
                logger.info("Retention purge removed old rows.", extra=deleted)
            else:
                logger.debug("Retention purge: nothing to remove.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Retention loop crashed: {e}")
            await asyncio.sleep(3600)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global PTB error handler.

    Network timeouts and transient connection errors are logged at WARNING level
    (they're expected during heavy sweeps and resolve on the next poll cycle).
    Conflict errors are also WARNING — they happen during Railway redeploys when
    the new container starts polling before the old one has fully exited, and
    self-resolve in <30s. Only persistent Conflicts indicate a real duplicate
    instance that needs operator attention.
    Everything else is logged at ERROR so real problems are still visible.
    """
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"Transient network error (ignored): {err}")
        return
    if isinstance(err, Conflict):
        logger.warning(
            "getUpdates Conflict — another bot instance is polling with the same "
            "token. If this persists for more than a minute, check Railway for "
            "duplicate services or a leftover local dev process."
        )
        return
    logger.error("Unhandled PTB exception", exc_info=err)


# The advertised command menu. Module-level so a test can assert it matches the
# handlers actually registered below — /importwhitelist was documented and named
# in the bot's own recovery message for months while never being registered.
BOT_COMMANDS = [
    BotCommand("import_admins",   "Whitelist all current group admins"),
    BotCommand("whitelist",       "Whitelist a user (reply or ID)"),
    BotCommand("unwhitelist",     "Remove from whitelist (reply or ID)"),
    BotCommand("listwhitelist",   "Show whitelist + download CSV"),
    BotCommand("importwhitelist", "Restore a whitelist from a CSV (reply to the file)"),
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
    BotCommand("setthresholds",   "Per-type thresholds (username=88 name=85)"),
    BotCommand("setbands",        "Set severity bands (e.g. /setbands 90 78)"),
    BotCommand("blocklist",       "Toggle cross-group blocklist (on/off)"),
    BotCommand("protect",         "Protect an external identity by name (+ photo)"),
    BotCommand("settings",        "Show this group's full configuration"),
    BotCommand("logs",            "Recent detections + admin actions"),
    BotCommand("clearwhitelist",  "⚠️ Remove all protected users (requires confirm)"),
]


async def _start_watcher(pyro_client, bot=None, log_channel_id=None):
    """
    Start the MTProto watcher, or return None so the bot runs Bot-API-only.

    This used to be a bare `await pyro_client.start()` sitting outside the
    try/finally and after polling had begun, so a revoked session, a malformed
    session string, or a non-integer api_id escaped main() with the getUpdates
    long-poll still open and nothing calling updater.stop(). That produced the
    `Conflict: terminated by other getUpdates request` churn the SIGTERM
    handling exists to prevent, then a 10x crash-loop and a dead service —
    even though group moderation is entirely Bot API and would have kept
    working.

    Degrading is the right outcome: bans, joins and message scans continue,
    only profile-change events and full sweeps are lost. health.py already has
    the matching terminal-session semantics.
    """
    if not pyro_client:
        return None

    try:
        await pyro_client.start()
    except Exception as e:
        logger.error(
            "Pyrogram watcher could not start — continuing WITHOUT it. "
            "Profile-change monitoring and full sweeps are unavailable; "
            "bans, joins and message scans are unaffected. "
            f"Regenerate PYROGRAM_SESSION if this persists. ({type(e).__name__}: {e})",
            exc_info=e,
        )
        if bot and log_channel_id:
            try:
                await bot.send_message(
                    chat_id=log_channel_id,
                    text=(
                        "⚠️ <b>Pyrogram watcher failed to start</b>\n"
                        f"<code>{html.escape(type(e).__name__)}: "
                        f"{html.escape(str(e)[:200])}</code>\n\n"
                        "Running in Bot-API-only mode: profile-change detection "
                        "and /sweep are DOWN. Joins and message scans still work. "
                        "Regenerate <code>PYROGRAM_SESSION</code> and redeploy."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                logger.warning("Could not report watcher failure to the log channel.")
        return None

    logger.info("Pyrogram client started.")

    # Warm up entity cache — without this, get_chat_members fails with
    # PEER_ID_INVALID for groups the session has never interacted with.
    # Best-effort: a cold cache costs a PEER_ID_INVALID on first sweep, which
    # is recoverable, so a warm-up failure must not cost us the watcher.
    logger.info("Warming up Pyrogram entity cache (iterating dialogs)…")
    try:
        async for _ in pyro_client.get_dialogs():
            pass
        logger.info("Entity cache ready.")
    except Exception as e:
        logger.warning(f"Could not warm up entity cache: {e}")

    return pyro_client


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
    app.add_handler(CommandHandler("setthresholds",   set_type_threshold))
    app.add_handler(CommandHandler("setbands",        set_bands))
    app.add_handler(CommandHandler("blocklist",       blocklist_toggle))
    app.add_handler(CommandHandler("protect",         protect_identity))
    app.add_handler(CommandHandler("settings",        settings))
    app.add_handler(CommandHandler("logs",            logs))
    app.add_handler(CommandHandler("clearwhitelist",  clear_whitelist_cmd))
    app.add_handler(CommandHandler("importwhitelist", import_whitelist))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("csv") & filters.ChatType.PRIVATE,
        import_whitelist,
    ))

    # Inline keyboard callbacks from log-channel detection alerts
    app.add_handler(CallbackQueryHandler(
        handle_detection_callback,
        pattern=r"^(unban_wl|unban_fp|dismiss|ban_now|kick_now)\|",
    ))
    # Undo button on /clearwhitelist success message
    app.add_handler(CallbackQueryHandler(
        handle_whitelist_undo, pattern=r"^wl_undo\|",
    ))
    # Inline pagination nav for /listwhitelist and /logs
    app.add_handler(CallbackQueryHandler(handle_whitelist_page, pattern=r"^wl_pg\|"))
    app.add_handler(CallbackQueryHandler(handle_logs_page,      pattern=r"^logs_pg\|"))

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
    # Bound the executor that db.run_db offloads onto. asyncio's default is
    # min(32, cpu+4) workers, which would let threads outnumber pooled
    # connections and queue up inside getconn — turning a connection shortage
    # into a pile of threads each waiting out the acquire timeout. One worker
    # per connection means a thread only ever waits on the query itself.
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(
            max_workers=DB_POOL_MAX_SIZE, thread_name_prefix="db",
        )
    )

    # Install the SIGTERM/SIGINT handler FIRST. Railway sends SIGTERM on
    # redeploy, and its default disposition kills the process instantly — the
    # finally block never runs and the old container's getUpdates long-poll
    # stays open, which is exactly what makes the new container log
    # "Conflict: terminated by other getUpdates request". Installed before any
    # slow startup work (init_db against a sleeping database, the entity-cache
    # warm-up) so a redeploy arriving mid-boot is still handled.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # add_signal_handler isn't supported on Windows' default loop;
            # KeyboardInterrupt still covers SIGINT there.
            pass

    init_db()

    # The cross-group blocklist only propagates bans from groups the operator
    # has explicitly trusted, because any admin of any enrolled group can write
    # to it via /ban and the bot enrols any group it is added to. Empty is the
    # safe default but it does turn propagation off, so say so once, loudly,
    # rather than letting an operator assume the feature is working.
    if BLOCKLIST_TRUSTED_GROUPS:
        logger.info(
            "Cross-group blocklist: bans propagate from "
            f"{len(BLOCKLIST_TRUSTED_GROUPS)} trusted group(s)."
        )
    else:
        logger.warning(
            "Cross-group blocklist propagation is DISABLED — no groups are "
            "listed in BLOCKLIST_TRUSTED_GROUPS. Manual bans still work per "
            "group, and pre-existing blocklist entries are treated as advisory "
            "(alert, never auto-ban). Set BLOCKLIST_TRUSTED_GROUPS to a "
            "comma-separated list of your own group IDs to enable propagation."
        )

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

    # The Pyrogram client deliberately does NOT go into bot_data. PTB snapshots
    # bot_data with copy.deepcopy on every persistence interval, and a Pyrogram
    # Client can't be deep-copied — that killed the persistence updater on its
    # first tick (so nothing was ever persisted) and then made Application.stop()
    # re-raise the TypeError, skipping shutdown() entirely. Handlers reach the
    # client through src.watcher.client.get_client(), which needs no pickling.
    # build_client() already registered it.

    # Register commands for both private chats and groups
    await ptb_app.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await ptb_app.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllGroupChats())

    # Start the watcher BEFORE polling. Previously polling began first and the
    # client was only started ~60s later, after a full get_dialogs walk — so any
    # update arriving in that window reached handlers that call get_client() and
    # got a client that wasn't connected yet. Those failures are swallowed at
    # debug level in fetch.py, so a member who joined during startup was checked
    # with no bio and no photo and nobody was told. Starting it first also means
    # a failure here happens before the getUpdates long-poll is open.
    pyro_client = await _start_watcher(pyro_client, ptb_app.bot, LOG_CHANNEL_ID)

    await ptb_app.start()
    await ptb_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    if LOG_CHANNEL_ID:
        try:
            pyro_status = (
                "✅ Pyrogram watcher active" if pyro_client
                else "⚠️ Pyrogram watcher disabled"
            )
            await ptb_app.bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=f"🟢 <b>Anti-Impersonator Bot started</b>\n{pyro_status}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Could not send startup message to log channel: {e}")

    logger.info("Bot is running.")

    async def _report_death(name: str, exc: BaseException | None) -> None:
        """
        Tell the operator a background task died and is being restarted.

        Best-effort: a failure here must not stop the restart, which is the part
        that actually recovers the bot.
        """
        if not LOG_CHANNEL_ID:
            return
        detail = (
            f"{type(exc).__name__}: {str(exc)[:300]}" if exc
            else "exited cleanly, which should never happen"
        )
        await ptb_app.bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=(
                f"⚠️ <b>Background task died:</b> <code>{html.escape(name)}</code>\n"
                f"<code>{html.escape(detail)}</code>\n"
                "It is being restarted automatically — no action needed unless "
                "this repeats. Check the logs for the traceback."
            ),
            parse_mode="HTML",
        )


    if pyro_client:
        sweep_task = asyncio.create_task(_supervised(
            "periodic_sweep",
            lambda: run_periodic_sweeps(pyro_client, ptb_app.bot, LOG_CHANNEL_ID),
            notify=_report_death,
        ))
        health_task = asyncio.create_task(_supervised(
            "health_check",
            lambda: run_health_check(pyro_client, ptb_app.bot, LOG_CHANNEL_ID),
            notify=_report_death,
        ))

    # DB keep-alive — prevents Railway Hobby Postgres from sleeping
    keepalive_task = asyncio.create_task(_supervised(
        "db_keepalive", _db_keepalive, notify=_report_death,
    ))

    # Retention — unconditional, unlike the daily summary it used to hide in.
    retention_task = asyncio.create_task(_supervised(
        "retention", _retention_loop, notify=_report_death,
    ))

    # Detects a loop that is alive but not running — the signature of a blocking
    # call finding its way back onto it. Nothing else in the process can see that.
    watchdog_task = asyncio.create_task(_supervised(
        "loop_watchdog", _loop_lag_watchdog,
    ))

    summary_task = None
    if LOG_CHANNEL_ID:
        from src.watcher.summary import run_daily_summary
        summary_task = asyncio.create_task(_supervised(
            "daily_summary",
            lambda: run_daily_summary(ptb_app.bot, LOG_CHANNEL_ID),
            notify=_report_death,
        ))

    try:
        await stop_event.wait()  # run until a shutdown signal arrives
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down…")
        # Stop polling FIRST so the getUpdates long-poll is released before the
        # new instance starts — this is what shrinks the redeploy Conflict window.
        try:
            await ptb_app.updater.stop()
        except Exception as e:
            logger.warning(f"updater.stop() failed: {e}")

        tasks = [keepalive_task, retention_task, watchdog_task]
        if summary_task:
            tasks.append(summary_task)
        if pyro_client:
            tasks.extend([sweep_task, health_task])
        for t in tasks:
            t.cancel()
        # Await the cancellations so task cleanup actually completes before we
        # tear down the loop (bare .cancel() doesn't wait).
        await asyncio.gather(*tasks, return_exceptions=True)

        if pyro_client:
            try:
                await pyro_client.stop()
            except Exception as e:
                logger.warning(f"pyro_client.stop() failed: {e}")
        await ptb_app.stop()
        await ptb_app.shutdown()
