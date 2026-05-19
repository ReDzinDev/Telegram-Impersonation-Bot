
import csv
import io
import logging

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, KeyboardButtonRequestChat, ReplyKeyboardRemove, InputFile
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus, ChatType

from src.db import (
    get_group, upsert_group,
    upsert_whitelisted_user, remove_whitelisted_user,
    set_group_check_mode, set_group_action_mode, set_group_log_channel,
    get_stats, get_latest_log_entry, get_whitelist, mark_seen,
)
from src.utils.image import compute_pfp_hash_bytes
from src.config import LOG_CHANNEL_ID

logger = logging.getLogger(__name__)

# ── Private-chat group context helpers ────────────────────────────────────────

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


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the command sender is an admin of the relevant group."""
    if update.effective_chat.type != ChatType.PRIVATE:
        member = await update.effective_chat.get_member(update.effective_user.id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]

    group_id = context.user_data.get("active_group_id")
    if not group_id:
        return False
    try:
        member = await context.bot.get_chat_member(group_id, update.effective_user.id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception:
        return False


async def _fetch_pfp(user) -> str | None:
    try:
        photos = await user.get_profile_photos(limit=1)
        if photos.total_count > 0:
            f = await photos.photos[0][-1].get_file()
            return compute_pfp_hash_bytes(bytes(await f.download_as_bytearray()))
    except Exception as e:
        logger.warning(f"Could not get PFP for {user.id}: {e}")
    return None


# ── /start ─────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        group_id    = context.user_data.get("active_group_id")
        group_title = context.user_data.get("active_group_title")

        group_line = (
            f"Active group: <b>{group_title}</b> (<code>{group_id}</code>)"
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
            "<b>Available commands:</b>\n"
            "/import_admins — Whitelist all current admins\n"
            "/whitelist — Whitelist a user (reply or ID)\n"
            "/unwhitelist — Remove a user from the whitelist\n"
            "/check — Manually check a user (reply or ID)\n"
            "/ban — Manually ban a user (reply or ID)\n"
            "/unban — Unban a user by ID\n"
            "/sweep — Run a full member scan\n"
            "/setmode strict|relaxed — Set message scan mode\n"
            "/setaction ban|kick|alert — Set detection action\n"
            "/setlogchannel — Set a per-group log channel\n"
            "/watch — Protect a non-admin VIP's identity\n"
            "/listwhitelist — Show all protected users\n"
            "/exportwhitelist — Download whitelist as CSV\n"
            "/stats — Show group protection stats",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(
                keyboard, resize_keyboard=True, one_time_keyboard=True
            ),
        )
    else:
        group_id = update.effective_chat.id
        group = get_group(group_id)
        mode = group["check_mode"] if group else "not registered"
        await update.message.reply_text(
            f"🛡 <b>Anti-Impersonator Bot active</b>\n"
            f"Mode: <code>{mode}</code>\n\n"
            "Use /import_admins to populate the whitelist.",
            parse_mode="HTML",
        )


# ── /start private group-picker callback ──────────────────────────────────────

async def handle_chat_shared(update: Update, context: ContextTypes.DEFAULT_TYPE):
    shared   = update.message.chat_shared
    group_id = shared.chat_id

    try:
        chat        = await context.bot.get_chat(group_id)
        group_title = chat.title or str(group_id)
    except Exception:
        group_title = str(group_id)

    context.user_data["active_group_id"]    = group_id
    context.user_data["active_group_title"] = group_title

    await update.message.reply_text(
        f"✅ <b>Active group:</b> {group_title}\n\n"
        "You can now run all commands from here and they'll apply to that group.\n"
        "Use /start to switch groups.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )

    ok, msg = await _import_admins_logic(group_id, update.effective_user.id, context)
    await update.message.reply_text(msg, parse_mode="HTML")


# ── /import_admins ─────────────────────────────────────────────────────────────

async def import_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ok, msg = await _import_admins_logic(group_id, update.effective_user.id, context)
    await update.message.reply_text(msg, parse_mode="HTML")


async def _import_admins_logic(chat_id: int, requester_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat   = await context.bot.get_chat(chat_id)
        admins = await chat.get_administrators()
    except Exception as e:
        return False, f"❌ Could not access the group. Is the bot an admin there? (<code>{e}</code>)"

    upsert_group(chat_id, title=chat.title)

    count = 0
    for admin in admins:
        user = admin.user
        if user.is_bot:
            continue
        pfp_hash = await _fetch_pfp(user)
        upsert_whitelisted_user(
            group_id=chat_id,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            pfp_hash=pfp_hash,
            whitelisted_by=requester_id,
        )
        mark_seen(chat_id, user.id)
        count += 1

    return True, f"✅ Imported/updated <b>{count}</b> admin(s) for <b>{chat.title or chat_id}</b>."


# ── /whitelist ─────────────────────────────────────────────────────────────────

async def whitelist_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    # In-group: reply to the target message
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif context.args:
        # Private or in-group with ID: look up via the bot
        try:
            target_id = int(context.args[0])
            member    = await context.bot.get_chat_member(group_id, target_id)
            target    = member.user
        except (ValueError, Exception) as e:
            await update.message.reply_text(
                f"❌ Could not find user: <code>{e}</code>\n"
                "Usage: reply to a message or /whitelist &lt;user_id&gt;",
                parse_mode="HTML",
            )
            return
    else:
        await update.message.reply_text("Reply to a user's message or provide a user ID.")
        return

    pfp_hash = await _fetch_pfp(target)
    upsert_whitelisted_user(
        group_id=group_id,
        user_id=target.id,
        username=target.username,
        first_name=target.first_name,
        last_name=target.last_name,
        pfp_hash=pfp_hash,
        whitelisted_by=update.effective_user.id,
    )
    mark_seen(group_id, target.id)
    await update.message.reply_text(
        f"✅ <b>{target.full_name}</b> has been whitelisted.", parse_mode="HTML"
    )


# ── /unwhitelist ───────────────────────────────────────────────────────────────

async def unwhitelist_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
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
        await update.message.reply_text(f"✅ User <code>{target_id}</code> removed from whitelist.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"User <code>{target_id}</code> was not in the whitelist.", parse_mode="HTML")


# ── /check ─────────────────────────────────────────────────────────────────────

async def check_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    from src.utils.checker import UserSnapshot, check_user

    # Resolve target from reply or user ID argument
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif context.args:
        try:
            target_id = int(context.args[0])
            member    = await context.bot.get_chat_member(group_id, target_id)
            target    = member.user
        except (ValueError, Exception) as e:
            await update.message.reply_text(
                f"❌ Could not find user: <code>{e}</code>\n"
                "Usage: reply to a message or /check &lt;user_id&gt;",
                parse_mode="HTML",
            )
            return
    else:
        await update.message.reply_text("Reply to a user's message or provide a user ID.")
        return

    pfp_bytes = None
    try:
        photos = await target.get_profile_photos(limit=1)
        if photos.total_count > 0:
            f         = await photos.photos[0][-1].get_file()
            pfp_bytes = bytes(await f.download_as_bytearray())
    except Exception:
        pass

    snapshot = UserSnapshot(
        user_id=target.id,
        username=target.username,
        first_name=target.first_name,
        last_name=target.last_name,
        pfp_bytes=pfp_bytes,
    )
    result = await check_user(snapshot, group_id)

    if result.flagged:
        await update.message.reply_text(
            f"⚠️ <b>Suspicious user detected</b>\n"
            f"Match type: <code>{result.match_type}</code>\n"
            f"Matched: <code>{result.matched_val}</code>\n"
            f"Score: <code>{result.score}</code>\n"
            f"Impersonating: <b>{result.target_name or 'Unknown'}</b>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"✅ <b>{target.full_name}</b> looks clean — no whitelist matches.", parse_mode="HTML"
        )


# ── /ban ───────────────────────────────────────────────────────────────────────

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
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
            await update.message.reply_text("Usage: /ban &lt;user_id&gt; or reply to a message.", parse_mode="HTML")
            return

    if not target_id:
        await update.message.reply_text("Reply to a message or provide a user ID.")
        return

    try:
        await context.bot.ban_chat_member(chat_id=group_id, user_id=target_id)
        from src.db import insert_log
        insert_log(
            group_id=group_id, user_id=target_id, username=None, full_name=None,
            target_user_id=None, target_name=None,
            detection_type="manual", similarity_score=None,
            action_taken="banned", details=f"Manual ban by {update.effective_user.id}",
            trigger="manual",
        )
        await update.message.reply_text(f"🚫 User <code>{target_id}</code> has been banned.", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to ban: <code>{e}</code>", parse_mode="HTML")


# ── /unban ─────────────────────────────────────────────────────────────────────

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /unban &lt;user_id&gt;", parse_mode="HTML")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Provide a numeric user ID.")
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    try:
        await context.bot.unban_chat_member(chat_id=group_id, user_id=target_id, only_if_banned=True)
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been unbanned.", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to unban: <code>{e}</code>", parse_mode="HTML")


# ── /sweep ─────────────────────────────────────────────────────────────────────

async def sweep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    pyro = context.bot_data.get("pyro_client")
    if not pyro:
        await update.message.reply_text(
            "⚠️ Sweep requires the Pyrogram watcher to be configured.\n"
            "Set PYROGRAM_API_ID, PYROGRAM_API_HASH, and PYROGRAM_SESSION in your environment."
        )
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    log_channel  = context.bot_data.get("log_channel_id") or LOG_CHANNEL_ID
    status_msg   = await update.message.reply_text("🔍 Sweep started… this may take a while.")

    from src.watcher.sweep import sweep_group

    async def progress(checked: int, flagged: int):
        try:
            await status_msg.edit_text(f"🔍 Sweeping… checked {checked} members, flagged {flagged}.")
        except Exception:
            pass

    result = await sweep_group(pyro, context.bot, group_id, log_channel, progress_cb=progress)

    if result.get("status") == "already_running":
        await status_msg.edit_text("⚠️ A sweep is already running for this group.")
        return

    await status_msg.edit_text(
        f"✅ <b>Sweep complete</b>\n"
        f"Checked: <code>{result.get('checked', 0)}</code>\n"
        f"Flagged & banned: <code>{result.get('flagged', 0)}</code>\n"
        f"Errors: <code>{result.get('errors', 0)}</code>",
        parse_mode="HTML",
    )


# ── /setmode ───────────────────────────────────────────────────────────────────

async def setmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    if not context.args or context.args[0].lower() not in ("strict", "relaxed"):
        await update.message.reply_text("Usage: /setmode strict|relaxed")
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx

    mode = context.args[0].lower()
    upsert_group(group_id, title=group_title)
    set_group_check_mode(group_id, mode)

    desc = (
        "every message sender is re-checked" if mode == "strict"
        else "each user is checked only on their first message"
    )
    await update.message.reply_text(
        f"✅ Scan mode set to <b>{mode}</b> — {desc}.", parse_mode="HTML"
    )


# ── /setaction ────────────────────────────────────────────────────────────────

async def setaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    valid = ("ban", "kick", "alert")
    if not context.args or context.args[0].lower() not in valid:
        await update.message.reply_text(
            "Usage: /setaction ban|kick|alert\n\n"
            "• ban — permanently ban the impersonator (default)\n"
            "• kick — remove without a permanent ban (can rejoin)\n"
            "• alert — notify only, no action taken"
        )
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx

    mode = context.args[0].lower()
    upsert_group(group_id, title=group_title)
    set_group_action_mode(group_id, mode)

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
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    rows = get_whitelist(group_id)
    if not rows:
        await update.message.reply_text("No protected users yet. Run /import_admins first.")
        return

    lines = []
    for r in rows:
        name  = f"{r['first_name']} {r['last_name'] or ''}".strip()
        uname = f"@{r['username']}" if r['username'] else "no username"
        kind  = r.get("user_type", "manual")
        lines.append(f"• <a href='tg://user?id={r['user_id']}'>{name}</a> ({uname}) — <i>{kind}</i>")

    header = f"🛡 <b>Protected users ({len(rows)})</b>\n\n"
    msg    = header + "\n".join(lines)
    if len(msg) <= 4096:
        await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)
    else:
        chunks, current = [], header
        for line in lines:
            if len(current) + len(line) + 1 > 4096:
                await update.message.reply_text(current, parse_mode="HTML", disable_web_page_preview=True)
                current = ""
            current += line + "\n"
        if current:
            await update.message.reply_text(current, parse_mode="HTML", disable_web_page_preview=True)


# ── /setlogchannel ────────────────────────────────────────────────────────────

async def set_log_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "  /setlogchannel &lt;channel_id&gt;\n"
            "  /setlogchannel clear\n\n"
            "Get the channel ID by forwarding a message from it to @userinfobot.",
            parse_mode="HTML",
        )
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx
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
            text=f"✅ Log channel set for group <b>{group_title}</b>.",
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
    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    group = get_group(group_id)
    s     = get_stats(group_id)

    if not s:
        await update.message.reply_text("No stats available yet.")
        return

    mode = group["check_mode"] if group else "unknown"
    action = group.get("action_mode", "ban") if group else "ban"
    await update.message.reply_text(
        f"📊 <b>Stats for this group</b>\n\n"
        f"Scan mode: <code>{mode}</code>\n"
        f"Action mode: <code>{action}</code>\n"
        f"Whitelisted users: <code>{s.get('whitelisted', 0)}</code>\n"
        f"Total detections: <code>{s.get('detections', 0)}</code>\n"
        f"Total bans: <code>{s.get('banned', 0)}</code>",
        parse_mode="HTML",
    )


# ── /watch ─────────────────────────────────────────────────────────────────────

async def watch_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Protect a non-admin VIP's identity without granting them any trust.

    Usage:
      /watch          — reply to a message from the user to watch
      /watch <id>     — watch by user ID (requires Pyrogram to resolve profile)
    """
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx
    upsert_group(group_id, title=group_title)

    # ── Case 1: reply to a message ─────────────────────────────────────────────
    if update.message.reply_to_message:
        target   = update.message.reply_to_message.from_user
        pfp_hash = await _fetch_pfp(target)
        upsert_whitelisted_user(
            group_id=group_id,
            user_id=target.id,
            username=target.username,
            first_name=target.first_name,
            last_name=target.last_name,
            pfp_hash=pfp_hash,
            whitelisted_by=update.effective_user.id,
            user_type="watch",
        )
        await update.message.reply_text(
            f"👁 <b>{target.full_name}</b> is now watched — impersonators will be banned.",
            parse_mode="HTML",
        )
        return

    # ── Case 2: /watch <user_id> via Pyrogram ──────────────────────────────────
    if not context.args:
        await update.message.reply_text(
            "Usage: reply to a message with /watch, or /watch &lt;user_id&gt;",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Provide a numeric user ID.")
        return

    pyro = context.bot_data.get("pyro_client")
    if not pyro:
        await update.message.reply_text(
            "⚠️ Looking up users by ID requires the Pyrogram watcher.\n"
            "Set PYROGRAM_API_ID, PYROGRAM_API_HASH, and PYROGRAM_SESSION, "
            "or use /watch as a reply to a message instead."
        )
        return

    try:
        pyro_user = await pyro.get_users(target_id)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not resolve user <code>{target_id}</code>: <code>{e}</code>", parse_mode="HTML"
        )
        return

    from io import BytesIO
    from src.utils.image import compute_pfp_hash_bytes as _hash_bytes
    pfp_hash = None
    try:
        buf = BytesIO()
        async for chunk in pyro.stream_media(
            await pyro.get_chat_photos(target_id, limit=1).__anext__()
        ):
            buf.write(chunk)
        pfp_hash = _hash_bytes(buf.getvalue())
    except Exception:
        pass

    username = None
    if getattr(pyro_user, "usernames", None):
        username = pyro_user.usernames[0].username
    else:
        username = getattr(pyro_user, "username", None)

    upsert_whitelisted_user(
        group_id=group_id,
        user_id=pyro_user.id,
        username=username,
        first_name=pyro_user.first_name or "",
        last_name=pyro_user.last_name,
        pfp_hash=pfp_hash,
        whitelisted_by=update.effective_user.id,
        user_type="watch",
    )
    full_name = f"{pyro_user.first_name or ''} {pyro_user.last_name or ''}".strip()
    await update.message.reply_text(
        f"👁 <b>{full_name}</b> (ID: <code>{pyro_user.id}</code>) is now watched.",
        parse_mode="HTML",
    )


# ── Detection alert inline buttons ────────────────────────────────────────────

async def handle_detection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles inline button presses on log-channel detection alerts.

    Callback data format: "<action>|<group_id>|<user_id>"
      unban_wl  — unban the user and add them to the whitelist
      dismiss   — remove the buttons without taking action
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

    if action == "unban_wl":
        try:
            await context.bot.unban_chat_member(
                chat_id=group_id, user_id=user_id, only_if_banned=True
            )
        except Exception as e:
            await query.answer(f"Unban failed: {e}", show_alert=True)
            return

        entry = get_latest_log_entry(group_id, user_id)
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

        await query.edit_message_reply_markup(reply_markup=None)
        await query.answer(f"Unbanned + whitelisted by {admin_name}.", show_alert=False)


# ── /exportwhitelist ──────────────────────────────────────────────────────────

async def export_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    rows = get_whitelist(group_id)
    if not rows:
        await update.message.reply_text("No protected users yet. Run /import_admins first.")
        return

    buf = io.StringIO()
    fieldnames = ["user_id", "username", "first_name", "last_name", "user_type", "created_at"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

    file_bytes = io.BytesIO(buf.getvalue().encode("utf-8"))
    await update.message.reply_document(
        document=InputFile(file_bytes, filename="whitelist.csv"),
        caption=f"Whitelist export — {len(rows)} protected user(s).",
    )
