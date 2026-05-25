
import csv
import html
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
    add_reserved_keyword, remove_reserved_keyword, get_reserved_keywords,
    set_group_threshold, get_recent_logs,
    log_admin_action, get_recent_admin_actions,
    clear_whitelist as db_clear_whitelist,
    get_all_group_stats,
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
            "<b>Available commands:</b>\n"
            "/import_admins — Whitelist all current admins\n"
            "/whitelist — Reply, or /whitelist 123456\n"
            "/unwhitelist — Reply, or /unwhitelist 123456\n"
            "/watch — Protect a VIP (reply or /watch 123456)\n"
            "/listwhitelist — Show all protected users\n"
            "/exportwhitelist — Download whitelist as CSV\n"
            "/clearwhitelist confirm — ⚠️ Remove all protected users\n"
            "/check — Reply, or /check 123456\n"
            "/ban — Reply, or /ban 123456\n"
            "/unban 123456 — Unban a user by ID\n"
            "/sweep — Full member scan (Pyrogram required)\n"
            "/setmode strict|relaxed — Default: relaxed\n"
            "/setaction ban|kick|alert — Default: ban\n"
            "/setlogchannel -100… — or /setlogchannel clear\n"
            "/addkeyword admin — or /addkeyword r:official.*admin for regex\n"
            "/removekeyword admin — Remove a reserved keyword\n"
            "/listkeywords — List all reserved keywords\n"
            "/setthreshold 85 — Fuzzy sensitivity (50–100, default 85)\n"
            "/stats — Stats (all groups shown in private chat)\n"
            "/logs 20 — Last N detections\n"
            "/auditlog 20 — Last N admin actions",
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
        f"✅ <b>Active group:</b> {html.escape(group_title)}\n\n"
        "You can now run all commands from here and they'll apply to that group.\n"
        "Use /start to switch groups.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )

    ok, msg = await _import_admins_logic(
        group_id, update.effective_user.id, update.effective_user.full_name, context
    )
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

    log_admin_action(
        group_id=chat_id,
        admin_id=requester_id,
        admin_name=requester_name,
        action="import_admins",
        details=f"Imported {count} admin(s)",
    )
    return True, f"✅ Imported/updated <b>{count}</b> admin(s) for <b>{html.escape(str(chat.title or chat_id))}</b>."


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
    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="whitelist",
        target_id=target.id,
        details=target.full_name,
    )
    await update.message.reply_text(
        f"✅ <b>{html.escape(target.full_name)}</b> has been whitelisted.", parse_mode="HTML"
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
        group_cfg   = get_group(group_id)
        action_mode = (group_cfg.get("action_mode", "ban") if group_cfg else None) or "ban"
        action_labels = {
            "ban":   "permanently banned",
            "kick":  "kicked (not permanently banned)",
            "alert": "alert only — no action taken",
        }
        await update.message.reply_text(
            f"⚠️ <b>Suspicious user detected</b>\n"
            f"Match type: <code>{result.match_type}</code>\n"
            f"Matched: <code>{html.escape(str(result.matched_val))}</code>\n"
            f"Score: <code>{result.score:.1f}</code>\n"
            f"Impersonating: <b>{html.escape(result.target_name or 'Unknown')}</b>\n"
            f"Action if triggered: <i>{action_labels.get(action_mode, action_mode)}</i>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"✅ <b>{html.escape(target.full_name)}</b> looks clean — no whitelist matches.", parse_mode="HTML"
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
        from src.db import insert_log
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

    checked = result.get("checked", 0)
    flagged = result.get("flagged", 0)
    errors  = result.get("errors", 0)
    note    = "\n<i>(Admins and already-whitelisted users are skipped.)</i>" if checked == 0 else ""

    await status_msg.edit_text(
        f"✅ <b>Sweep complete</b>\n"
        f"Checked: <code>{checked}</code>\n"
        f"Flagged & banned: <code>{flagged}</code>\n"
        f"Errors: <code>{errors}</code>{note}",
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
    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="setmode",
        details=mode,
    )

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
        name  = html.escape(f"{r['first_name']} {r['last_name'] or ''}".strip())
        uname = f"@{html.escape(r['username'])}" if r['username'] else "no username"
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
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    # Private chat: show a breakdown of every registered group
    if update.effective_chat.type == ChatType.PRIVATE:
        all_gs = get_all_group_stats()
        if not all_gs:
            await update.message.reply_text("No groups registered yet.")
            return

        total_protected  = sum(g.get("whitelisted", 0)  for g in all_gs)
        total_detections = sum(g.get("detections", 0)   for g in all_gs)
        total_bans       = sum(g.get("banned", 0)       for g in all_gs)

        lines = [f"📊 <b>Stats across {len(all_gs)} group(s)</b>\n"]
        for gs in all_gs:
            title = html.escape(gs.get("title") or str(gs["group_id"]))
            lines.append(
                f"<b>{title}</b>\n"
                f"  🛡 {gs.get('whitelisted', 0)} protected · "
                f"🚨 {gs.get('detections', 0)} detections · "
                f"🚫 {gs.get('banned', 0)} bans · "
                f"mode: {gs.get('check_mode', '?')}/{gs.get('action_mode', '?')}"
            )

        lines.append(
            f"\n<b>Total:</b> {total_protected} protected · "
            f"{total_detections} detections · {total_bans} bans"
        )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    # Group chat: show this group's stats
    group_id = update.effective_chat.id
    group    = get_group(group_id)
    s        = get_stats(group_id)

    if not s:
        await update.message.reply_text("No stats available yet.")
        return

    mode      = group["check_mode"]  if group else "unknown"
    action    = group.get("action_mode", "ban") if group else "ban"
    threshold = group.get("similarity_threshold") if group else None
    thr_label = f"<code>{threshold}</code>" if threshold else "<code>85</code> (default)"

    await update.message.reply_text(
        f"📊 <b>Stats for this group</b>\n\n"
        f"Scan mode: <code>{mode}</code>\n"
        f"Action mode: <code>{action}</code>\n"
        f"Similarity threshold: {thr_label}\n"
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
        log_admin_action(
            group_id=group_id,
            admin_id=update.effective_user.id,
            admin_name=update.effective_user.full_name,
            action="watch",
            target_id=target.id,
            details=target.full_name,
        )
        await update.message.reply_text(
            f"👁 <b>{html.escape(target.full_name)}</b> is now watched — impersonators will be banned.",
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
    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="watch",
        target_id=pyro_user.id,
        details=full_name,
    )
    await update.message.reply_text(
        f"👁 <b>{html.escape(full_name)}</b> (ID: <code>{pyro_user.id}</code>) is now watched.",
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


# ── /addkeyword ───────────────────────────────────────────────────────────────

async def add_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Add a reserved keyword or regex pattern for this group.

    Usage:
      /addkeyword admin            — plain keyword (case-insensitive)
      /addkeyword r:official.*ceo  — regex (prefix with r:)
    """
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx
    upsert_group(group_id, title=group_title)

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "  /addkeyword admin             — plain keyword\n"
            "  /addkeyword r:official.*admin — regex (prefix with <code>r:</code>)\n\n"
            "Matches against display name, username, and bio.",
            parse_mode="HTML",
        )
        return

    raw_pattern = " ".join(context.args)
    is_regex = raw_pattern.startswith("r:")
    pattern  = raw_pattern[2:].strip() if is_regex else raw_pattern.strip()

    if is_regex:
        import re
        try:
            re.compile(pattern)
        except re.error as e:
            await update.message.reply_text(
                f"❌ Invalid regex: <code>{e}</code>", parse_mode="HTML"
            )
            return

    add_reserved_keyword(group_id, pattern, is_regex, update.effective_user.id)
    kind = "regex" if is_regex else "keyword"
    await update.message.reply_text(
        f"✅ {kind.capitalize()} <code>{pattern}</code> added — "
        "any non-whitelisted user whose name, username, or bio matches will be flagged.",
        parse_mode="HTML",
    )


# ── /removekeyword ────────────────────────────────────────────────────────────

async def remove_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
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
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
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
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

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

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx
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


# ── /logs ─────────────────────────────────────────────────────────────────────

async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent detection log entries. Usage: /logs [limit=10]"""
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    limit = 10
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 50))
        except ValueError:
            pass

    rows = get_recent_logs(group_id, limit)
    if not rows:
        await update.message.reply_text("No detections logged for this group yet.")
        return

    lines = []
    for r in rows:
        dt     = r["created_at"].strftime("%m-%d %H:%M") if r["created_at"] else "?"
        name   = html.escape(r["full_name"] or r["username"] or f"ID {r['user_id']}")
        target = html.escape(r["target_name"] or "?")
        dtype  = r["detection_type"] or "?"
        action = r["action_taken"] or "?"
        lines.append(
            f"<b>{dt}</b> — <a href='tg://user?id={r['user_id']}'>{name}</a> "
            f"→ {target} | <i>{dtype}</i> | {action}"
        )

    header  = f"📋 <b>Last {len(rows)} detections</b>\n\n"
    chunks  = []
    current = header
    for line in lines:
        if len(current) + len(line) + 1 > 4096:
            chunks.append(current.rstrip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)


# ── /importwhitelist ──────────────────────────────────────────────────────────

async def import_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Upload a CSV file to bulk-add users to the whitelist.
    Expected columns: user_id, username, first_name, last_name
    (same format as /exportwhitelist output).
    """
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx
    upsert_group(group_id, title=group_title)

    doc = update.message.document
    if not doc:
        await update.message.reply_text(
            "Send a CSV file as a document with this command.\n"
            "Expected columns: <code>user_id, username, first_name, last_name</code>\n\n"
            "Use /exportwhitelist to download the current whitelist as a template.",
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
            user_type="manual",
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
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
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


# ── /auditlog ─────────────────────────────────────────────────────────────────

async def audit_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent admin actions for this group. Usage: /auditlog [limit=20]"""
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can use this command.")
        return

    ctx = await _get_active_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    limit = 20
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 50))
        except ValueError:
            pass

    rows = get_recent_admin_actions(group_id, limit)
    if not rows:
        await update.message.reply_text(
            "No admin actions logged for this group yet.\n"
            "Actions like /whitelist, /ban, /setmode, /watch are recorded here."
        )
        return

    lines = []
    for r in rows:
        dt     = r["created_at"].strftime("%m-%d %H:%M") if r["created_at"] else "?"
        who    = html.escape(r["admin_name"] or f"ID {r['admin_id']}")
        action = r["action"]
        target = f" → <code>{r['target_id']}</code>" if r["target_id"] else ""
        detail = f" ({html.escape(r['details'])})" if r["details"] else ""
        lines.append(f"<b>{dt}</b> {who} — {action}{target}{detail}")

    header  = f"🔍 <b>Last {len(rows)} admin actions</b>\n\n"
    chunks  = []
    current = header
    for line in lines:
        if len(current) + len(line) + 1 > 4096:
            chunks.append(current.rstrip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())

    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)
