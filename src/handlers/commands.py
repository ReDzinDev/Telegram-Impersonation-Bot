
import csv
import html
import io
import logging
import time as _time

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, KeyboardButtonRequestChat, ReplyKeyboardRemove, InputFile
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus, ChatType

from src.db import (
    get_group, upsert_group,
    upsert_whitelisted_user, remove_whitelisted_user,
    set_group_action_mode, set_group_log_channel,
    get_stats_windowed, get_latest_log_entry, get_whitelist, mark_seen,
    add_reserved_keyword, remove_reserved_keyword, get_reserved_keywords,
    set_group_threshold, get_recent_logs,
    log_admin_action, get_recent_admin_actions, insert_log,
    clear_whitelist as db_clear_whitelist,
    get_all_group_stats_windowed,
    mark_false_positive,
)
from src.utils.image import compute_pfp_hash_bytes
from src.config import LOG_CHANNEL_ID

logger = logging.getLogger(__name__)

# ── Private-chat group context helpers ────────────────────────────────────────

# 5-minute admin status cache: (user_id, group_id) → (expires_monotonic, is_admin)
_admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}
_ADMIN_CACHE_TTL = 300  # seconds


async def _get_active_group(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[int, str] | None:
    """
    Returns (group_id, group_title) for the current command context.

    In a group chat this is always the chat itself.
    In private chat it is the group the user previously selected via the
    group picker.  If no group is selected yet, sends the picker prompt
    and returns None so the caller can early-return.
    """
    if update.effective_chat.type != ChatType.PRIVATE:
        return update.effective_chat.id, update.effective_chat.title or ""

    group_id = context.user_data.get("active_group_id")
    if not group_id:
        await _send_group_picker(update)
        return None
    return group_id, context.user_data.get("active_group_title", str(group_id))


async def _send_group_picker(update: Update):
    keyboard = [[KeyboardButton(
        "Select a group",
        request_chat=KeyboardButtonRequestChat(
            request_id=1,
            chat_is_channel=False,
            bot_is_member=True,
        ),
    )]]
    await update.message.reply_text(
        "No group selected yet. Pick the group you want to manage:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True),
    )


async def _is_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    group_id: int | None = None,
) -> bool:
    """
    Check if the command sender is an admin of the relevant group.

    Results are cached for 5 minutes to avoid repeated getChatMember API calls.
    Pass group_id explicitly when you already know it (avoids a second context lookup).
    """
    user_id = update.effective_user.id

    if update.effective_chat.type != ChatType.PRIVATE:
        gid = update.effective_chat.id
    else:
        gid = group_id or context.user_data.get("active_group_id")
        if not gid:
            return False

    # Cache hit
    cache_key = (user_id, gid)
    now = _time.monotonic()
    if cache_key in _admin_cache:
        expires_at, cached = _admin_cache[cache_key]
        if now < expires_at:
            return cached

    # Live API call
    try:
        if update.effective_chat.type != ChatType.PRIVATE:
            member = await update.effective_chat.get_member(user_id)
        else:
            member = await context.bot.get_chat_member(gid, user_id)
        result = member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception as e:
        logger.warning(f"Admin check failed for user {user_id} in group {gid}: {e}")
        result = False

    _admin_cache[cache_key] = (now + _ADMIN_CACHE_TTL, result)
    return result


async def _get_admin_group(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[int, str] | None:
    """
    Combined helper: resolve the active group then verify the caller is an admin.

    Returns (group_id, group_title) on success.
    Returns None after sending the appropriate message to the user (group picker
    or "Only admins" error) so callers can simply do ``if not ctx: return``.
    """
    ctx = await _get_active_group(update, context)
    if not ctx:
        return None  # _get_active_group already sent the group picker

    group_id, group_title = ctx
    if not await _is_admin(update, context, group_id=group_id):
        await update.message.reply_text("Only admins can use this command.")
        return None

    return group_id, group_title


async def _fetch_pfp(user) -> str | None:
    try:
        photos = await user.get_profile_photos(limit=1)
        if photos.total_count > 0:
            f = await photos.photos[0][-1].get_file()
            return compute_pfp_hash_bytes(bytes(await f.download_as_bytearray()))
    except Exception as e:
        logger.warning(f"Could not get PFP for {user.id}: {e}")
    return None


async def _fetch_group_pfp_hash(bot, chat) -> str | None:
    """Download and hash the group's current profile photo. Returns None if unavailable."""
    try:
        if not chat.photo:
            return None
        f = await bot.get_file(chat.photo.big_file_id)
        raw = bytes(await f.download_as_bytearray())
        return compute_pfp_hash_bytes(raw)
    except Exception as e:
        logger.warning(f"Could not fetch group PFP for {chat.id}: {e}")
    return None


# ── /start ─────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        group_id    = context.user_data.get("active_group_id")
        group_title = context.user_data.get("active_group_title")

        group_line = (
            f"Active group: <b>{html.escape(str(group_title))}</b> (<code>{group_id}</code>)"
            if group_id else
            "No group selected yet."
        )
        btn_label = "Switch Group" if group_id else "Select Group"

        keyboard = [[KeyboardButton(
            btn_label,
            request_chat=KeyboardButtonRequestChat(
                request_id=1,
                chat_is_channel=False,
                bot_is_member=True,
            ),
        )]]
        await update.message.reply_text(
            f"👋 <b>Anti-Impersonator Bot</b>\n\n"
            f"{group_line}\n\n"
            "<b>Whitelist</b>\n"
            "/import_admins — Whitelist all current admins\n"
            "/whitelist — Reply, or /whitelist 123456\n"
            "/unwhitelist — Reply, or /unwhitelist 123456\n"
            "/listwhitelist — Show whitelist + download CSV\n"
            "/clearwhitelist confirm — ⚠️ Remove all protected users\n"
            "\n<b>Moderation</b>\n"
            "/ban — Reply, or /ban 123456\n"
            "/unban 123456 — Unban a user by ID\n"
            "/sweep — Full member scan (Pyrogram required)\n"
            "\n<b>Configuration</b>\n"
            "/setaction ban|kick|alert — Default: ban\n"
            "/setlogchannel — Pick the log channel (or /setlogchannel clear)\n"
            "/setthreshold 85 — Fuzzy sensitivity (50–100, default 85)\n"
            "/addkeyword admin, *mod*, support* — keywords, wildcards, regex (r:...)\n"
            "/removekeyword admin — Remove a keyword\n"
            "/listkeywords — List reserved keywords\n"
            "\n<b>Insights</b>\n"
            "/stats — All-time, 30d, 7d breakdown\n"
            "/logs 20 — Recent detections + admin actions",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(
                keyboard, resize_keyboard=True, one_time_keyboard=True
            ),
        )
    else:
        group_id = update.effective_chat.id
        group = get_group(group_id)
        action = (group.get("action_mode", "ban") if group else "not registered")
        await update.message.reply_text(
            f"🛡 <b>Anti-Impersonator Bot active</b>\n"
            f"Action mode: <code>{action}</code>\n\n"
            "Use /import_admins to populate the whitelist.",
            parse_mode="HTML",
        )


# ── /start private group-picker callback ──────────────────────────────────────

async def handle_chat_shared(update: Update, context: ContextTypes.DEFAULT_TYPE):
    shared     = update.message.chat_shared
    request_id = shared.request_id
    chat_id    = shared.chat_id

    # ── request_id=1: group picker (active group selection) ──────────────────
    if request_id == 1:
        try:
            chat        = await context.bot.get_chat(chat_id)
            group_title = chat.title or str(chat_id)
        except Exception:
            group_title = str(chat_id)

        context.user_data["active_group_id"]    = chat_id
        context.user_data["active_group_title"] = group_title

        await update.message.reply_text(
            f"✅ <b>Active group:</b> {html.escape(group_title)}\n\n"
            "You can now run all commands from here and they'll apply to that group.\n"
            "Use /start to switch groups.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )

        ok, msg = await _import_admins_logic(
            chat_id, update.effective_user.id, update.effective_user.full_name, context
        )
        await update.message.reply_text(msg, parse_mode="HTML")
        return

    # ── request_id=2: log channel picker ─────────────────────────────────────
    if request_id == 2:
        # Resolve the group we're configuring
        g_id    = context.user_data.get("active_group_id")
        g_title = context.user_data.get("active_group_title", str(g_id) if g_id else "?")

        if not g_id:
            await update.message.reply_text(
                "No active group selected. Use /start to pick a group first.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        # Verify the bot can post to the channel
        try:
            channel_chat  = await context.bot.get_chat(chat_id)
            channel_title = channel_chat.title or str(chat_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Log channel set for group <b>{html.escape(g_title)}</b>.",
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Could not post to that channel: <code>{html.escape(str(e))}</code>\n\n"
                "Make sure the bot is an admin in the channel and has permission to post messages.",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        upsert_group(g_id, title=g_title)
        set_group_log_channel(g_id, chat_id)
        await update.message.reply_text(
            f"✅ Log channel set to <b>{html.escape(channel_title)}</b> "
            f"(<code>{chat_id}</code>) for <b>{html.escape(g_title)}</b>.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )


# ── /import_admins ─────────────────────────────────────────────────────────────

async def import_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    ok, msg = await _import_admins_logic(
        group_id, update.effective_user.id, update.effective_user.full_name, context
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def _import_admins_logic(
    chat_id: int, requester_id: int, requester_name: str,
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        chat   = await context.bot.get_chat(chat_id)
        admins = await chat.get_administrators()
    except Exception as e:
        return False, f"❌ Could not access the group. Is the bot an admin there? (<code>{e}</code>)"

    # Store the group's own PFP so the bot can detect impersonators of the group itself
    group_pfp_hash = await _fetch_group_pfp_hash(context.bot, chat)
    upsert_group(chat_id, title=chat.title, pfp_hash=group_pfp_hash)

    count      = 0
    bot_count  = 0
    for admin in admins:
        user = admin.user
        # Skip the Anti-Impersonator Bot itself, but keep other admin bots
        # (Rose, Combot, etc.) so their names/usernames are also protected.
        if user.id == context.bot.id:
            continue
        pfp_hash = await _fetch_pfp(user) if not user.is_bot else None
        upsert_whitelisted_user(
            group_id=chat_id,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name or "",
            pfp_hash=pfp_hash,
            whitelisted_by=requester_id,
            user_type="admin",
            is_bot=bool(user.is_bot),
        )
        mark_seen(chat_id, user.id)
        count += 1
        if user.is_bot:
            bot_count += 1

    log_admin_action(
        group_id=chat_id,
        admin_id=requester_id,
        admin_name=requester_name,
        action="import_admins",
        details=f"Imported {count} admin(s) ({bot_count} bot(s))",
    )
    bot_note = f", including <b>{bot_count}</b> bot(s)" if bot_count else ""
    return True, (
        f"✅ Imported/updated <b>{count}</b> admin(s){bot_note} "
        f"for <b>{html.escape(str(chat.title or chat_id))}</b>."
    )


# ── /whitelist ─────────────────────────────────────────────────────────────────

async def whitelist_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Add a user to the protected list.

    Resolution order:
      1. Reply to a message  → use that user.
      2. /whitelist <id>     → Bot API getChatMember; if that fails (user
                                not in the chat yet, etc.), fall back to
                                the Pyrogram userbot when available.

    This replaces the old /watch command — there was no behavioural
    difference, just a cosmetic user_type tag, which is now always
    'manual' for hand-added entries.
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    target = None              # PTB User (when resolved via Bot API)
    pyro_user = None           # Pyrogram user (when resolved via MTProto)

    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                "Usage: reply to a message, or /whitelist &lt;user_id&gt;",
                parse_mode="HTML",
            )
            return

        # Try Bot API first — fastest and works for users in the chat
        try:
            member = await context.bot.get_chat_member(group_id, target_id)
            target = member.user
        except Exception:
            # Fall back to the Pyrogram userbot — required for users
            # who haven't joined the group yet (proactive whitelisting)
            pyro = context.bot_data.get("pyro_client")
            if not pyro:
                await update.message.reply_text(
                    f"❌ Could not find user <code>{target_id}</code> in this group.\n"
                    "Enable the Pyrogram watcher to whitelist users by ID without "
                    "them being in the chat.",
                    parse_mode="HTML",
                )
                return
            try:
                pyro_user = await pyro.get_users(target_id)
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Could not resolve user <code>{target_id}</code>: <code>{e}</code>",
                    parse_mode="HTML",
                )
                return
    else:
        await update.message.reply_text("Reply to a user's message or provide a user ID.")
        return

    # Normalize fields + fetch PFP hash from whichever client resolved the user
    if target is not None:
        user_id    = target.id
        username   = target.username
        first_name = target.first_name
        last_name  = target.last_name
        full_name  = target.full_name
        is_bot     = bool(target.is_bot)
        pfp_hash   = await _fetch_pfp(target)
    else:
        # Pyrogram path
        user_id    = pyro_user.id
        # Pyrogram surfaces usernames as a list on multi-username accounts
        if getattr(pyro_user, "usernames", None):
            username = pyro_user.usernames[0].username
        else:
            username = getattr(pyro_user, "username", None)
        first_name = pyro_user.first_name or ""
        last_name  = pyro_user.last_name
        full_name  = f"{first_name} {last_name or ''}".strip()
        is_bot     = bool(getattr(pyro_user, "is_bot", False))
        pfp_hash   = await _fetch_pfp_pyro(context.bot_data["pyro_client"], user_id)

    upsert_whitelisted_user(
        group_id=group_id,
        user_id=user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        pfp_hash=pfp_hash,
        whitelisted_by=update.effective_user.id,
        user_type="manual",
        is_bot=is_bot,
    )
    mark_seen(group_id, user_id)
    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="whitelist",
        target_id=user_id,
        details=full_name,
    )
    await update.message.reply_text(
        f"✅ <b>{html.escape(full_name)}</b> has been whitelisted.", parse_mode="HTML"
    )


async def _fetch_pfp_pyro(pyro, user_id: int) -> str | None:
    """Download and hash a user's PFP via the Pyrogram userbot."""
    from io import BytesIO
    try:
        photo = await pyro.get_chat_photos(user_id, limit=1).__anext__()
        buf = BytesIO()
        async for chunk in pyro.stream_media(photo):
            buf.write(chunk)
        return compute_pfp_hash_bytes(buf.getvalue())
    except Exception:
        return None


# ── /unwhitelist ───────────────────────────────────────────────────────────────

async def unwhitelist_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /unwhitelist &lt;user_id&gt; or reply to a message.", parse_mode="HTML")
            return

    if not target_id:
        await update.message.reply_text("Reply to a message or provide a user ID.")
        return

    removed = remove_whitelisted_user(group_id, target_id)
    if removed:
        log_admin_action(
            group_id=group_id,
            admin_id=update.effective_user.id,
            admin_name=update.effective_user.full_name,
            action="unwhitelist",
            target_id=target_id,
        )
        await update.message.reply_text(f"✅ User <code>{target_id}</code> removed from whitelist.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"User <code>{target_id}</code> was not in the whitelist.", parse_mode="HTML")


# ── /ban ───────────────────────────────────────────────────────────────────────

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    target_user = None
    target_id   = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_id   = target_user.id
    elif context.args:
        try:
            target_id = int(context.args[0])
            # Try to resolve user details so the log entry is complete
            try:
                member    = await context.bot.get_chat_member(group_id, target_id)
                target_user = member.user
            except Exception:
                pass
        except ValueError:
            await update.message.reply_text("Usage: /ban &lt;user_id&gt; or reply to a message.", parse_mode="HTML")
            return

    if not target_id:
        await update.message.reply_text("Reply to a message or provide a user ID.")
        return

    try:
        await context.bot.ban_chat_member(chat_id=group_id, user_id=target_id)
        insert_log(
            group_id=group_id,
            user_id=target_id,
            username=target_user.username if target_user else None,
            full_name=target_user.full_name if target_user else None,
            target_user_id=None, target_name=None,
            detection_type="manual", similarity_score=None,
            action_taken="banned",
            details=f"Manual ban by {update.effective_user.full_name} ({update.effective_user.id})",
            trigger="manual",
        )
        log_admin_action(
            group_id=group_id,
            admin_id=update.effective_user.id,
            admin_name=update.effective_user.full_name,
            action="ban",
            target_id=target_id,
            details=target_user.full_name if target_user else None,
        )
        await update.message.reply_text(f"🚫 User <code>{target_id}</code> has been banned.", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to ban: <code>{e}</code>", parse_mode="HTML")


# ── /unban ─────────────────────────────────────────────────────────────────────

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    if not context.args:
        await update.message.reply_text("Usage: /unban &lt;user_id&gt;", parse_mode="HTML")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Provide a numeric user ID.")
        return

    try:
        await context.bot.unban_chat_member(chat_id=group_id, user_id=target_id, only_if_banned=True)
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been unbanned.", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to unban: <code>{e}</code>", parse_mode="HTML")


# ── /sweep ─────────────────────────────────────────────────────────────────────

async def sweep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    pyro = context.bot_data.get("pyro_client")
    if not pyro:
        await update.message.reply_text(
            "⚠️ Sweep requires the Pyrogram watcher to be configured.\n"
            "Set PYROGRAM_API_ID, PYROGRAM_API_HASH, and PYROGRAM_SESSION in your environment."
        )
        return

    log_channel  = context.bot_data.get("log_channel_id") or LOG_CHANNEL_ID
    status_msg   = await update.message.reply_text("🔍 Sweep started — fetching member list…")

    from src.watcher.sweep import sweep_group

    async def progress(iterated: int, checked: int, flagged: int):
        try:
            if iterated == 0:
                text = "🔍 Sweep running — scanning members…"
            else:
                text = (
                    f"🔍 Sweeping… {iterated} seen · "
                    f"{checked} checked · {flagged} flagged"
                )
            await status_msg.edit_text(text)
        except Exception:
            pass

    try:
        result = await sweep_group(pyro, context.bot, group_id, log_channel, progress_cb=progress)
    except Exception as e:
        logger.error(f"Sweep command error for {group_id}: {e}")
        await status_msg.edit_text(f"❌ Sweep failed: <code>{e}</code>", parse_mode="HTML")
        return

    if result.get("status") == "already_running":
        await status_msg.edit_text(
            "⚠️ A background sweep is already running for this group. Try again in a moment."
        )
        return

    iterated = result.get("iterated", 0)
    checked  = result.get("checked", 0)
    flagged  = result.get("flagged", 0)
    errors   = result.get("errors", 0)
    note     = "\n<i>(All members were admins or already whitelisted.)</i>" if checked == 0 else ""

    await status_msg.edit_text(
        f"✅ <b>Sweep complete</b>\n"
        f"Members seen: <code>{iterated}</code>\n"
        f"Checked (non-whitelisted): <code>{checked}</code>\n"
        f"Flagged & actioned: <code>{flagged}</code>\n"
        f"Errors: <code>{errors}</code>{note}",
        parse_mode="HTML",
    )


# ── /setaction ────────────────────────────────────────────────────────────────

async def setaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx

    valid = ("ban", "kick", "alert")
    if not context.args or context.args[0].lower() not in valid:
        await update.message.reply_text(
            "Usage: /setaction ban|kick|alert\n\n"
            "• ban — permanently ban the impersonator (default)\n"
            "• kick — remove without a permanent ban (can rejoin)\n"
            "• alert — notify only, no action taken"
        )
        return

    mode = context.args[0].lower()
    upsert_group(group_id, title=group_title)
    set_group_action_mode(group_id, mode)
    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="setaction",
        details=mode,
    )

    desc = {
        "ban":   "impersonators will be permanently banned",
        "kick":  "impersonators will be removed (not permanently banned)",
        "alert": "detections are logged and notified — no action taken",
    }[mode]
    await update.message.reply_text(
        f"✅ Action mode set to <b>{mode}</b> — {desc}.", parse_mode="HTML"
    )


# ── /listwhitelist ────────────────────────────────────────────────────────────

async def list_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Show the protected users, split into Admins / Bots / Manual sections,
    and attach a CSV export so admins don't need a separate command.
    (This used to be two commands: /listwhitelist and /exportwhitelist.)
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    rows = get_whitelist(group_id)
    if not rows:
        await update.message.reply_text("No protected users yet. Run /import_admins first.")
        return

    # Partition rows into the three buckets we report on. is_bot is
    # authoritative (set from User.is_bot at write time); we no longer
    # need the username-ends-in-'bot' heuristic.
    admins:  list[dict] = []
    bots:    list[dict] = []
    manual:  list[dict] = []
    for r in rows:
        if r.get("is_bot"):
            bots.append(r)
        elif r.get("user_type") == "admin":
            admins.append(r)
        else:
            manual.append(r)

    def _fmt(r: dict) -> str:
        name  = html.escape(f"{r['first_name']} {r['last_name'] or ''}".strip())
        uname = f"@{html.escape(r['username'])}" if r['username'] else "no username"
        return f"• <a href='tg://user?id={r['user_id']}'>{name}</a> ({uname})"

    header = (
        f"🛡 <b>Protected users — {len(rows)} total</b>\n"
        f"<i>{len(admins)} admins · {len(bots)} bots · {len(manual)} manual</i>\n"
    )

    sections = []
    if admins:
        sections.append("\n👑 <b>Admins</b>\n" + "\n".join(_fmt(r) for r in admins))
    if bots:
        sections.append("\n🤖 <b>Bots</b>\n" + "\n".join(_fmt(r) for r in bots))
    if manual:
        sections.append("\n✋ <b>Manual</b>\n" + "\n".join(_fmt(r) for r in manual))

    # Telegram caps messages at 4096 chars — chunk by section line
    body = header + "".join(sections)
    if len(body) <= 4096:
        await update.message.reply_text(body, parse_mode="HTML", disable_web_page_preview=True)
    else:
        current = header
        for section in sections:
            if len(current) + len(section) + 1 > 4096:
                await update.message.reply_text(current, parse_mode="HTML", disable_web_page_preview=True)
                current = ""
            current += section
        if current.strip():
            await update.message.reply_text(current, parse_mode="HTML", disable_web_page_preview=True)

    # Attach the CSV export — same payload the old /exportwhitelist produced.
    buf = io.StringIO()
    fieldnames = ["user_id", "username", "first_name", "last_name", "user_type", "is_bot", "created_at"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    file_bytes = io.BytesIO(buf.getvalue().encode("utf-8"))
    await update.message.reply_document(
        document=InputFile(file_bytes, filename="whitelist.csv"),
        caption=f"CSV export — {len(rows)} protected user(s).",
    )


# ── /setlogchannel ────────────────────────────────────────────────────────────

async def set_log_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx

    if not context.args:
        # In private chat: offer the channel picker button for easy selection
        if update.effective_chat.type == ChatType.PRIVATE:
            keyboard = [[KeyboardButton(
                "Select log channel",
                request_chat=KeyboardButtonRequestChat(
                    request_id=2,
                    chat_is_channel=True,
                    bot_is_member=True,
                ),
            )]]
            await update.message.reply_text(
                "Pick the channel you want to use as the log channel for "
                f"<b>{html.escape(group_title)}</b>.\n\n"
                "The bot must already be an admin in that channel.",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True),
            )
        else:
            await update.message.reply_text(
                "Usage:\n"
                "  /setlogchannel &lt;channel_id&gt;\n"
                "  /setlogchannel clear\n\n"
                "Tip: DM me and use /setlogchannel there to get a channel picker button.",
                parse_mode="HTML",
            )
        return
    upsert_group(group_id, title=group_title)

    if context.args[0].lower() == "clear":
        set_group_log_channel(group_id, None)
        await update.message.reply_text("✅ Log channel cleared — falling back to global setting.")
        return

    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "Provide a numeric channel ID (e.g. <code>-1001234567890</code>).", parse_mode="HTML"
        )
        return

    try:
        await context.bot.send_message(
            chat_id=channel_id,
            text=f"✅ Log channel set for group <b>{html.escape(group_title)}</b>.",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not post to <code>{channel_id}</code>: <code>{e}</code>\n\n"
            "Make sure the bot is an admin in that channel.",
            parse_mode="HTML",
        )
        return

    set_group_log_channel(group_id, channel_id)
    await update.message.reply_text(
        f"✅ Log channel set to <code>{channel_id}</code>.", parse_mode="HTML"
    )


# ── /stats ─────────────────────────────────────────────────────────────────────

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Show stats with All-time / Last 30d / Last 7d breakdowns.

    In a group chat: stats for that group.
    In a private chat: per-group rollup across every registered group.
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return

    # Private chat: per-group rollup with All / 30d / 7d windows
    if update.effective_chat.type == ChatType.PRIVATE:
        all_gs = get_all_group_stats_windowed()
        if not all_gs:
            await update.message.reply_text("No groups registered yet.")
            return

        # Aggregate totals across every group, per window
        tot = {
            "protected":      sum(g.get("whitelisted", 0)      for g in all_gs),
            "detections_all": sum(g.get("detections_all", 0)   for g in all_gs),
            "detections_30d": sum(g.get("detections_30d", 0)   for g in all_gs),
            "detections_7d":  sum(g.get("detections_7d", 0)    for g in all_gs),
            "banned_all":     sum(g.get("banned_all", 0)       for g in all_gs),
            "banned_30d":     sum(g.get("banned_30d", 0)       for g in all_gs),
            "banned_7d":      sum(g.get("banned_7d", 0)        for g in all_gs),
        }

        lines = [f"📊 <b>Stats across {len(all_gs)} group(s)</b>\n"]
        for gs in all_gs:
            title = html.escape(gs.get("title") or str(gs["group_id"]))
            lines.append(
                f"<b>{title}</b> · action: {gs.get('action_mode', '?')}\n"
                f"  🛡 protected: {gs.get('whitelisted', 0)}\n"
                f"  🚨 detections — all: {gs.get('detections_all', 0)} · "
                f"30d: {gs.get('detections_30d', 0)} · "
                f"7d: {gs.get('detections_7d', 0)}\n"
                f"  🚫 bans — all: {gs.get('banned_all', 0)} · "
                f"30d: {gs.get('banned_30d', 0)} · "
                f"7d: {gs.get('banned_7d', 0)}"
            )

        lines.append(
            f"\n<b>Totals</b> · 🛡 {tot['protected']} protected\n"
            f"  🚨 detections — all: {tot['detections_all']} · "
            f"30d: {tot['detections_30d']} · 7d: {tot['detections_7d']}\n"
            f"  🚫 bans — all: {tot['banned_all']} · "
            f"30d: {tot['banned_30d']} · 7d: {tot['banned_7d']}"
        )
        # Chunk for the 4096-char cap
        msg = "\n".join(lines)
        for i in range(0, len(msg), 4000):
            await update.message.reply_text(msg[i:i+4000], parse_mode="HTML")
        return

    # Group chat: windowed breakdown
    group_id = update.effective_chat.id
    group    = get_group(group_id)
    s        = get_stats_windowed(group_id)

    if not s:
        await update.message.reply_text("No stats available yet.")
        return

    action    = (group.get("action_mode", "ban") if group else "ban") or "ban"
    threshold = group.get("similarity_threshold") if group else None
    thr_label = f"<code>{threshold}</code>" if threshold else "<code>85</code> (default)"

    def _row(label: str, det: int, banned: int, sweeps: int) -> str:
        return (
            f"<b>{label}</b>\n"
            f"  🚨 detections: <code>{det}</code> · "
            f"🚫 bans: <code>{banned}</code> · "
            f"🧹 sweeps: <code>{sweeps}</code>"
        )

    await update.message.reply_text(
        f"📊 <b>Stats for this group</b>\n\n"
        f"Action mode: <code>{action}</code>\n"
        f"Similarity threshold: {thr_label}\n"
        f"🛡 Protected users: <code>{s.get('whitelisted', 0)}</code>\n\n"
        + _row("All time",   s.get("detections_all", 0), s.get("banned_all", 0), s.get("sweeps_all", 0)) + "\n"
        + _row("Last 30 days", s.get("detections_30d", 0), s.get("banned_30d", 0), s.get("sweeps_30d", 0)) + "\n"
        + _row("Last 7 days",  s.get("detections_7d", 0),  s.get("banned_7d", 0),  s.get("sweeps_7d", 0)),
        parse_mode="HTML",
    )


# ── Detection alert inline buttons ────────────────────────────────────────────

async def handle_detection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles inline button presses on log-channel detection alerts.

    Callback data format: "<action>|<group_id>|<user_id>"
      unban_wl  — unban the user and add them to the whitelist
      unban_fp  — unban with a 30-day false-positive grace
      dismiss   — remove the buttons without taking action
      ban_now   — escalate an alert-mode detection to a permanent ban
      kick_now  — escalate an alert-mode detection to a kick (no ban)
    """
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    if len(parts) != 3:
        return

    action, group_id, user_id = parts[0], int(parts[1]), int(parts[2])
    admin_name = query.from_user.full_name

    if action == "dismiss":
        await query.edit_message_reply_markup(reply_markup=None)
        return

    # Escalation actions from alert-mode detections
    if action in ("ban_now", "kick_now"):
        try:
            await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
            if action == "kick_now":
                # Telegram kick = ban + immediate unban so the user can rejoin
                await context.bot.unban_chat_member(
                    chat_id=group_id, user_id=user_id, only_if_banned=True
                )
        except Exception as e:
            await query.answer(f"Action failed: {e}", show_alert=True)
            return

        verb = "banned" if action == "ban_now" else "kicked"
        # Mirror the manual /ban path so the detection log reflects the outcome
        entry = get_latest_log_entry(group_id, user_id)
        insert_log(
            group_id=group_id,
            user_id=user_id,
            username=(entry or {}).get("username"),
            full_name=(entry or {}).get("full_name"),
            target_user_id=None, target_name=None,
            detection_type="manual_escalation", similarity_score=None,
            action_taken=verb,
            details=f"Escalated from alert by {admin_name} ({query.from_user.id})",
            trigger="alert_escalation",
        )
        log_admin_action(
            group_id=group_id,
            admin_id=query.from_user.id,
            admin_name=admin_name,
            action=verb,
            target_id=user_id,
            details="from alert button",
        )
        await query.edit_message_reply_markup(reply_markup=None)
        await query.answer(f"{verb.capitalize()} by {admin_name}.", show_alert=False)
        return

    if action == "unban_wl":
        # only_if_banned=True is safe for alert-mode detections (no-op if not banned)
        try:
            await context.bot.unban_chat_member(
                chat_id=group_id, user_id=user_id, only_if_banned=True
            )
        except Exception as e:
            await query.answer(f"Action failed: {e}", show_alert=True)
            return

        entry = get_latest_log_entry(group_id, user_id)
        was_banned = entry and entry.get("action_taken") in ("banned", "kicked")
        if entry:
            name_parts = (entry.get("full_name") or "").split(maxsplit=1)
            first_name = name_parts[0] if name_parts else "Unknown"
            last_name  = name_parts[1] if len(name_parts) > 1 else None
            username   = entry.get("username")
        else:
            first_name, last_name, username = "Unknown", None, None

        upsert_whitelisted_user(
            group_id=group_id,
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            pfp_hash=None,
            whitelisted_by=query.from_user.id,
            user_type="manual",
        )

        # Audit-log the action so /logs reflects who reversed what
        log_admin_action(
            group_id=group_id,
            admin_id=query.from_user.id,
            admin_name=admin_name,
            action="unban_whitelist" if was_banned else "whitelist",
            target_id=user_id,
            details="from alert button",
        )

        await query.edit_message_reply_markup(reply_markup=None)
        msg = (
            f"Unbanned + whitelisted by {admin_name}." if was_banned
            else f"Whitelisted by {admin_name} (alert cleared)."
        )
        await query.answer(msg, show_alert=False)

    if action == "unban_fp":
        # only_if_banned=True makes this safe for alert-mode (no-op if not banned)
        try:
            await context.bot.unban_chat_member(
                chat_id=group_id, user_id=user_id, only_if_banned=True
            )
        except Exception as e:
            await query.answer(f"Action failed: {e}", show_alert=True)
            return

        entry = get_latest_log_entry(group_id, user_id)
        was_banned = entry and entry.get("action_taken") in ("banned", "kicked")
        mark_false_positive(group_id, user_id, cleared_by=query.from_user.id, days=30)
        log_admin_action(
            group_id=group_id,
            admin_id=query.from_user.id,
            admin_name=admin_name,
            action="unban_grace" if was_banned else "false_positive_grace",
            target_id=user_id,
            details="30-day grace, from alert button",
        )
        await query.edit_message_reply_markup(reply_markup=None)
        msg = (
            f"Unbanned (30-day grace) by {admin_name}." if was_banned
            else f"Alert ignored — 30-day grace set by {admin_name}."
        )
        await query.answer(msg, show_alert=False)


# ── /addkeyword ───────────────────────────────────────────────────────────────

async def add_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Add one or more reserved keywords / regex patterns for this group.

    Multiple entries can be supplied in a single command separated by commas.
    Each entry is one of:
      admin           — substring match (default, case-insensitive)
      admin*          — starts-with `admin`
      *admin          — ends-with `admin`
      *admin*         — explicit "contains" (same as bare `admin`)
      r:official.*ceo — Python regex (prefix with `r:`)

    Examples:
      /addkeyword admin, support, *ceo*
      /addkeyword admin*, r:official.*team
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx
    upsert_group(group_id, title=group_title)

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "  /addkeyword admin                       — substring match\n"
            "  /addkeyword admin*                      — starts-with\n"
            "  /addkeyword *admin                      — ends-with\n"
            "  /addkeyword r:official.*admin           — regex (prefix <code>r:</code>)\n"
            "  /addkeyword admin, support, *mod*       — multiple at once\n\n"
            "Matches are case-insensitive and checked against display name, "
            "username, and bio.",
            parse_mode="HTML",
        )
        return

    # Split on commas so a single command can register many keywords.
    raw = " ".join(context.args)
    entries = [e.strip() for e in raw.split(",") if e.strip()]

    if not entries:
        await update.message.reply_text("No keywords provided.")
        return

    import re as _re
    added:    list[str] = []
    failed:   list[tuple[str, str]] = []

    for entry in entries:
        is_regex = entry.startswith("r:")
        pattern  = entry[2:].strip() if is_regex else entry

        if is_regex:
            try:
                _re.compile(pattern)
            except _re.error as e:
                failed.append((entry, f"invalid regex: {e}"))
                continue

        ok = add_reserved_keyword(group_id, pattern, is_regex, update.effective_user.id)
        if ok:
            added.append(f"{'regex' if is_regex else 'keyword'} <code>{html.escape(pattern)}</code>")
        else:
            failed.append((entry, "DB error"))

    parts = []
    if added:
        parts.append(f"✅ Added {len(added)}: " + ", ".join(added))
    if failed:
        parts.append("⚠️ Skipped:")
        for e, why in failed:
            parts.append(f"  • <code>{html.escape(e)}</code> — {html.escape(why)}")

    await update.message.reply_text("\n".join(parts), parse_mode="HTML")


# ── /removekeyword ────────────────────────────────────────────────────────────

async def remove_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    if not context.args:
        await update.message.reply_text("Usage: /removekeyword &lt;pattern&gt;", parse_mode="HTML")
        return

    pattern = " ".join(context.args)
    if pattern.startswith("r:"):
        pattern = pattern[2:].strip()

    removed = remove_reserved_keyword(group_id, pattern)
    if removed:
        await update.message.reply_text(f"✅ Keyword <code>{pattern}</code> removed.", parse_mode="HTML")
    else:
        await update.message.reply_text(
            f"⚠️ <code>{pattern}</code> not found in keyword list.", parse_mode="HTML"
        )


# ── /listkeywords ─────────────────────────────────────────────────────────────

async def list_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    rows = get_reserved_keywords(group_id)
    if not rows:
        await update.message.reply_text(
            "No reserved keywords set.\n"
            "Use /addkeyword to add terms like <code>admin</code>, <code>support</code>, etc.",
            parse_mode="HTML",
        )
        return

    lines = []
    for r in rows:
        tag = "regex" if r["is_regex"] else "keyword"
        lines.append(f"• <code>{r['pattern']}</code> <i>({tag})</i>")

    await update.message.reply_text(
        f"🔑 <b>Reserved keywords ({len(rows)})</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


# ── /setthreshold ─────────────────────────────────────────────────────────────

async def set_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Set the fuzzy-match similarity threshold for this group (default: 85).
    Lower = more detections (more false positives).
    Higher = stricter (may miss subtle impersonation).
    Recommended range: 75–92.
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx

    if not context.args:
        await update.message.reply_text(
            "Usage: /setthreshold &lt;number&gt;\n\n"
            "Sets the similarity threshold for name/username matching.\n"
            "Default: <code>85</code> — recommended range: <code>75</code>–<code>92</code>.\n"
            "Lower = more sensitive, higher = more strict.",
            parse_mode="HTML",
        )
        return

    try:
        val = int(context.args[0])
        if not (50 <= val <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Provide an integer between 50 and 100.")
        return
    upsert_group(group_id, title=group_title)
    set_group_threshold(group_id, val)
    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="setthreshold",
        details=str(val),
    )

    await update.message.reply_text(
        f"✅ Similarity threshold set to <code>{val}</code>.", parse_mode="HTML"
    )


# ── /logs (detections + admin actions, merged) ───────────────────────────────

async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Show recent activity for the group: both detections AND admin actions
    in a single reply. Usage: /logs [limit=10]

    (This used to be two commands: /logs and /auditlog.)
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    limit = 10
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 50))
        except ValueError:
            pass

    # Note: limit is applied PER section. /logs 5 → up to 5 detections + 5 admin actions.
    detections = get_recent_logs(group_id, limit)
    actions    = get_recent_admin_actions(group_id, limit)

    if not detections and not actions:
        await update.message.reply_text(
            "No activity logged for this group yet.\n"
            "Detections appear here when an impersonator is caught; admin actions "
            "(whitelist / ban / setaction etc.) are recorded too."
        )
        return

    parts = []

    if detections:
        parts.append(f"📋 <b>Last {len(detections)} detections</b>")
        for r in detections:
            dt     = r["created_at"].strftime("%m-%d %H:%M") if r["created_at"] else "?"
            name   = html.escape(r["full_name"] or r["username"] or f"ID {r['user_id']}")
            target = html.escape(r["target_name"] or "?")
            dtype  = r["detection_type"] or "?"
            action = r["action_taken"] or "?"
            parts.append(
                f"<b>{dt}</b> — <a href='tg://user?id={r['user_id']}'>{name}</a> "
                f"→ {target} | <i>{dtype}</i> | {action}"
            )

    if actions:
        if parts:
            parts.append("")  # blank line between sections
        parts.append(f"🔍 <b>Last {len(actions)} admin actions</b>")
        for r in actions:
            dt     = r["created_at"].strftime("%m-%d %H:%M") if r["created_at"] else "?"
            who    = html.escape(r["admin_name"] or f"ID {r['admin_id']}")
            tgt    = f" → <code>{r['target_id']}</code>" if r["target_id"] else ""
            detail = f" ({html.escape(r['details'])})" if r["details"] else ""
            parts.append(f"<b>{dt}</b> {who} — {r['action']}{tgt}{detail}")

    # Chunk per Telegram's 4096-char message cap
    chunks  = []
    current = ""
    for line in parts:
        addition = line + "\n"
        if len(current) + len(addition) > 4096:
            chunks.append(current.rstrip())
            current = addition
        else:
            current += addition
    if current.strip():
        chunks.append(current.rstrip())
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)


# ── /importwhitelist ──────────────────────────────────────────────────────────

async def import_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Upload a CSV file to bulk-add users to the whitelist.
    Expected columns: user_id, username, first_name, last_name
    (same format as the CSV emitted by /listwhitelist).
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx
    upsert_group(group_id, title=group_title)

    doc = update.message.document
    if not doc:
        await update.message.reply_text(
            "Send a CSV file as a document with this command.\n"
            "Expected columns: <code>user_id, username, first_name, last_name</code>\n\n"
            "Use /listwhitelist to download the current whitelist as a template.",
            parse_mode="HTML",
        )
        return

    if not doc.file_name or not doc.file_name.endswith(".csv"):
        await update.message.reply_text("Please send a .csv file.")
        return

    try:
        file = await context.bot.get_file(doc.file_id)
        raw_bytes = await file.download_as_bytearray()
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        await update.message.reply_text(f"❌ Could not download file: <code>{e}</code>", parse_mode="HTML")
        return

    reader = csv.DictReader(io.StringIO(text))
    required = {"user_id", "first_name"}
    if not required.issubset(set(reader.fieldnames or [])):
        await update.message.reply_text(
            f"❌ CSV must have at least: <code>user_id, first_name</code>", parse_mode="HTML"
        )
        return

    added        = 0
    skipped      = 0
    failed_rows  = []
    for i, row in enumerate(reader, start=2):  # row 1 is the header
        try:
            uid = int(row["user_id"])
        except (ValueError, KeyError):
            skipped += 1
            bad_val = row.get("user_id", "<missing>")
            failed_rows.append(f"Row {i}: invalid user_id ({bad_val!r})")
            continue
        upsert_whitelisted_user(
            group_id=group_id,
            user_id=uid,
            username=row.get("username") or None,
            first_name=row.get("first_name") or "Unknown",
            last_name=row.get("last_name") or None,
            pfp_hash=None,
            whitelisted_by=update.effective_user.id,
            user_type=row.get("user_type") or "manual",
            is_bot=str(row.get("is_bot", "")).strip().lower() in ("true", "1", "yes"),
        )
        mark_seen(group_id, uid)
        added += 1

    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="importwhitelist",
        details=f"Imported {added}, skipped {skipped}",
    )
    await update.message.reply_text(
        f"✅ Imported <b>{added}</b> user(s) into whitelist for <b>{group_title}</b>."
        + (f"\n⚠️ Skipped {skipped} invalid row(s)." if skipped else ""),
        parse_mode="HTML",
    )
    if failed_rows:
        detail = "Failed rows:\n" + "\n".join(failed_rows[:20])
        if len(failed_rows) > 20:
            detail += f"\n…and {len(failed_rows) - 20} more."
        await update.message.reply_text(f"<pre>{detail}</pre>", parse_mode="HTML")


# ── /clearwhitelist ───────────────────────────────────────────────────────────

async def clear_whitelist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Remove ALL protected users from this group's whitelist.
    Requires /clearwhitelist confirm to execute (safety gate).
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx

    if not context.args or context.args[0].lower() != "confirm":
        count = len(get_whitelist(group_id))
        await update.message.reply_text(
            f"⚠️ <b>This will remove all {count} protected user(s) from the whitelist.</b>\n\n"
            "The bot will no longer detect impersonators until you re-run "
            "/import_admins or add users back manually.\n\n"
            "To proceed: /clearwhitelist confirm",
            parse_mode="HTML",
        )
        return

    count = db_clear_whitelist(group_id)
    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="clearwhitelist",
        details=f"Removed {count} user(s)",
    )
    await update.message.reply_text(
        f"✅ Whitelist cleared — <b>{count}</b> user(s) removed.\n"
        "Run /import_admins to repopulate.",
        parse_mode="HTML",
    )


