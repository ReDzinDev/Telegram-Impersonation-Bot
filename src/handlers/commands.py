
import csv
import html
import io
import logging
import time as _time

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, KeyboardButtonRequestChat, ReplyKeyboardRemove, InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus, ChatType

from src.db import (
    get_group, upsert_group,
    upsert_whitelisted_user, remove_whitelisted_user, remove_stale_admin_whitelist,
    set_group_action_mode, set_group_log_channel,
    get_stats_windowed, get_latest_log_entry, get_whitelist, mark_seen,
    add_reserved_keyword, remove_reserved_keyword, get_reserved_keywords,
    set_group_threshold, get_recent_logs,
    log_admin_action, get_recent_admin_actions, insert_log,
    clear_whitelist as db_clear_whitelist,
    get_all_group_stats_windowed,
    mark_false_positive,
    set_group_thresholds, set_group_score_bands, set_group_blocklist,
    add_known_bad_actor, get_known_bad_actor, remove_known_bad_actor,
)
from src.utils.image import compute_pfp_hash_bytes
from src.config import (
    LOG_CHANNEL_ID,
    NAME_SIMILARITY_THRESHOLD,
    USERNAME_SIMILARITY_THRESHOLD,
    DEFAULT_BAN_SCORE,
    DEFAULT_ALERT_SCORE,
)

logger = logging.getLogger(__name__)

# Module-level snapshot for /clearwhitelist undo (keyed by group_id).
# Populated right before the DB wipe; cleared after a successful undo.
_clearwhitelist_undo: dict[int, list[dict]] = {}

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

    if result:
        # Only cache positive results. Caching a transient get_chat_member
        # failure (network blip) as False would lock a real admin out of every
        # command for the full TTL.
        _admin_cache[cache_key] = (now + _ADMIN_CACHE_TTL, result)
    return result


async def _is_admin_of_group(
    context: ContextTypes.DEFAULT_TYPE, group_id: int, user_id: int
) -> bool:
    """
    Verify a user is an admin/owner of a specific group by user_id + group_id.

    Used for inline-button callbacks, where update.effective_chat is the log
    channel (not the moderated group) so _is_admin would check the wrong chat.
    The group_id here comes from untrusted callback data, so this MUST be
    called before acting on any callback that moderates a group. Shares
    _admin_cache with _is_admin.
    """
    cache_key = (user_id, group_id)
    now = _time.monotonic()
    if cache_key in _admin_cache:
        expires_at, cached = _admin_cache[cache_key]
        if now < expires_at:
            return cached
    try:
        member = await context.bot.get_chat_member(group_id, user_id)
        result = member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception as e:
        logger.warning(f"Admin check failed for user {user_id} in group {group_id}: {e}")
        return False  # don't cache transient failures
    if result:
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


def _resolve_log_channel(group_id: int, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Per-group log channel → global bot_data fallback → global env fallback."""
    group = get_group(group_id)
    return (
        (group and group.get("log_channel_id"))
        or context.bot_data.get("log_channel_id")
        or LOG_CHANNEL_ID
    )


async def _post_to_log_channel(
    context: ContextTypes.DEFAULT_TYPE, group_id: int, text: str,
) -> None:
    """Send an HTML message to the group's resolved log channel. No-op if none
    configured. Routes through src.utils.notify so consecutive failures
    against the same channel surface as a single global warning, rather
    than disappearing into the per-call log line."""
    channel = _resolve_log_channel(group_id, context)
    if not channel:
        return
    from src.utils.notify import send_log_message
    await send_log_message(context.bot, channel, text)


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
            "/protect Full Name — Protect an external identity (no account needed)\n"
            "\n<b>Moderation</b>\n"
            "/ban — Reply, or /ban 123456\n"
            "/unban 123456 — Unban a user by ID\n"
            "/sweep — Full member scan (Pyrogram required)\n"
            "\n<b>Configuration</b>\n"
            "/settings — Overview of all settings for this group\n"
            "/setaction ban|kick|alert — Default: ban\n"
            "/setlogchannel — Pick the log channel (or /setlogchannel clear)\n"
            "/setthreshold 85 — Global fuzzy sensitivity (50–100, default 85)\n"
            "/setthresholds username=88 name=85 — Per-type thresholds\n"
            "/setbands 90 78 — Set ban/alert score band thresholds\n"
            "/blocklist on|off — Cross-group blocklist participation\n"
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

        # The chat picker offers any group the bot is a member of, so the
        # selector isn't necessarily an admin. Selecting an active group is
        # harmless (every command re-checks _is_admin), but the auto-import
        # below writes whitelist rows — gate it on real admin status.
        if not await _is_admin_of_group(context, chat_id, update.effective_user.id):
            await update.message.reply_text(
                "You're not an admin of that group, so I can't set it up. "
                "Ask a group admin to run /start and select it.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

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

        # The CHAT_SHARED update itself is unauthenticated (could arrive from a
        # stale keyboard); confirm the caller still admins the target group.
        if not await _is_admin_of_group(context, g_id, update.effective_user.id):
            await update.message.reply_text(
                "Only admins of the active group can set its log channel.",
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
    """
    /import_admins         — add/refresh the current admin list
    /import_admins refresh — also prune admin-typed whitelist entries for
                              users who are no longer in the admin list
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    refresh = bool(context.args) and context.args[0].lower() in ("refresh", "--refresh", "prune")

    ok, msg = await _import_admins_logic(
        group_id, update.effective_user.id, update.effective_user.full_name,
        context, refresh=refresh,
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def _import_admins_logic(
    chat_id: int, requester_id: int, requester_name: str,
    context: ContextTypes.DEFAULT_TYPE,
    refresh: bool = False,
):
    try:
        chat   = await context.bot.get_chat(chat_id)
        admins = await chat.get_administrators()
    except Exception as e:
        return False, f"❌ Could not access the group. Is the bot an admin there? (<code>{e}</code>)"

    # Store the group's own PFP so the bot can detect impersonators of the group itself
    group_pfp_hash = await _fetch_group_pfp_hash(context.bot, chat)
    upsert_group(chat_id, title=chat.title, pfp_hash=group_pfp_hash)

    count             = 0
    bot_count         = 0
    current_admin_ids: set[int] = set()
    for admin in admins:
        user = admin.user
        # Skip the Anti-Impersonator Bot itself.
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
        current_admin_ids.add(user.id)
        count += 1
        if user.is_bot:
            bot_count += 1

    # The Bot API's getChatAdministrators deliberately omits *other bots*, so
    # admin bots (Rose, Combot, …) never appear in the loop above. Backfill
    # them via MTProto, which does return bot admins. Requires the Pyrogram
    # userbot to be a member of the group (same assumption as /sweep).
    pyro = context.bot_data.get("pyro_client")
    mtproto_note = ""
    if not pyro:
        mtproto_note = (
            "\n⚠️ <i>Pyrogram userbot not configured — admin bots can't be "
            "imported (Bot API hides them). Add bots manually with /whitelist.</i>"
        )
        logger.warning("import_admins: pyro_client not configured; skipping bot-admin backfill.")
    else:
        try:
            from pyrogram.enums import ChatMembersFilter
            mt_admins = 0
            mt_bots   = 0
            async for m in pyro.get_chat_members(
                chat_id, filter=ChatMembersFilter.ADMINISTRATORS
            ):
                mt_admins += 1
                u = m.user
                if not u or not u.is_bot:
                    continue
                mt_bots += 1
                if u.id == context.bot.id or u.id in current_admin_ids:
                    continue
                username = (
                    u.usernames[0].username if getattr(u, "usernames", None)
                    else getattr(u, "username", None)
                )
                upsert_whitelisted_user(
                    group_id=chat_id,
                    user_id=u.id,
                    username=username,
                    first_name=u.first_name or "",
                    last_name=u.last_name or "",
                    pfp_hash=None,
                    whitelisted_by=requester_id,
                    user_type="admin",
                    is_bot=True,
                )
                mark_seen(chat_id, u.id)
                current_admin_ids.add(u.id)
                count += 1
                bot_count += 1
            logger.info(
                f"import_admins MTProto backfill for {chat_id}: "
                f"{mt_admins} admin(s) seen, {mt_bots} bot(s)."
            )
            if mt_admins == 0:
                mtproto_note = (
                    "\n⚠️ <i>The userbot returned 0 admins via MTProto — it's "
                    "likely not a member of this group. Add it to the group so "
                    "it can see the admin bots.</i>"
                )
        except Exception as e:
            logger.warning(
                f"Could not enumerate admin bots via MTProto for {chat_id}: {e}"
            )
            mtproto_note = (
                f"\n⚠️ <i>Couldn't read admin bots via MTProto: "
                f"{html.escape(str(e))}</i>"
            )

    # Refresh mode: remove admin-typed rows for users no longer in the
    # admin list. Manual entries are untouched — only `user_type='admin'`
    # rows are pruned. This is opt-in because a "former admin" may still
    # be someone you want to protect against impersonation.
    pruned = 0
    if refresh:
        pruned = remove_stale_admin_whitelist(chat_id, current_admin_ids)

    log_admin_action(
        group_id=chat_id,
        admin_id=requester_id,
        admin_name=requester_name,
        action="import_admins" + (" (refresh)" if refresh else ""),
        details=(
            f"Imported {count} admin(s) ({bot_count} bot(s))"
            + (f", pruned {pruned} stale" if refresh else "")
        ),
    )
    bot_note   = f", including <b>{bot_count}</b> bot(s)" if bot_count else ""
    prune_note = f"\n🧹 Pruned <b>{pruned}</b> former admin(s) from the whitelist." if refresh and pruned else (
        "\n<i>No stale admin entries found.</i>" if refresh else ""
    )
    return True, (
        f"✅ Imported/updated <b>{count}</b> admin(s){bot_note} "
        f"for <b>{html.escape(str(chat.title or chat_id))}</b>."
        f"{prune_note}{mtproto_note}"
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
    """Download and hash a user's PFP via the Pyrogram userbot (shared helper)."""
    from src.watcher.fetch import fetch_pfp_hash
    return await fetch_pfp_hash(pyro, user_id)


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
    """
    Manual ban — reply, by ID, or by @username (Pyrogram-resolved).

    On success, posts a short notice to the group's log channel so the
    action shows up alongside automatic detections in the audit trail.
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    target_user      = None      # PTB-resolved User, when we have one
    target_username  = None      # for @username resolution via Pyrogram
    target_id        = None

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_id   = target_user.id
    elif context.args:
        arg = context.args[0].lstrip()
        # @username path — needs Pyrogram (Bot API can't resolve handles)
        if arg.startswith("@"):
            pyro = context.bot_data.get("pyro_client")
            if not pyro:
                await update.message.reply_text(
                    "⚠️ Resolving @usernames requires the Pyrogram watcher. "
                    "Use a numeric user ID, or enable Pyrogram in env.",
                    parse_mode="HTML",
                )
                return
            try:
                pyro_user = await pyro.get_users(arg)
                target_id        = pyro_user.id
                target_username  = (
                    pyro_user.usernames[0].username if getattr(pyro_user, "usernames", None)
                    else getattr(pyro_user, "username", None)
                )
                # Synthesize a minimal user-like obj for logging fields below
                target_user = type("U", (), {
                    "id":        pyro_user.id,
                    "username":  target_username,
                    "full_name": f"{pyro_user.first_name or ''} {pyro_user.last_name or ''}".strip(),
                })()
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Could not resolve <code>{html.escape(arg)}</code>: <code>{e}</code>",
                    parse_mode="HTML",
                )
                return
        else:
            try:
                target_id = int(arg)
                # Try Bot API for richer log entry; OK if it fails (e.g. user not in chat)
                try:
                    member      = await context.bot.get_chat_member(group_id, target_id)
                    target_user = member.user
                except Exception:
                    pass
            except ValueError:
                await update.message.reply_text(
                    "Usage: /ban &lt;user_id&gt; · /ban @username · or reply to a message.",
                    parse_mode="HTML",
                )
                return

    if not target_id:
        await update.message.reply_text("Reply to a message or provide a user ID / @username.")
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

        # Record in the cross-group blocklist (human-confirmed path only)
        add_known_bad_actor(
            user_id=target_id,
            username=target_user.username if target_user else None,
            full_name=target_user.full_name if target_user else None,
            reason="manual ban",
            confirmed_by=update.effective_user.id,
            source_group_id=group_id,
        )

        # Post to the group's log channel so it shows up alongside auto-detections
        admin = update.effective_user
        target_display = (
            f"<a href='tg://user?id={target_id}'>{html.escape(target_user.full_name)}</a>"
            if target_user and target_user.full_name
            else f"<code>{target_id}</code>"
        )
        await _post_to_log_channel(
            context, group_id,
            f"🚫 <b>Manual ban</b>\n"
            f"<b>User:</b> {target_display} | ID: <code>{target_id}</code>\n"
            f"<b>By:</b> <a href='tg://user?id={admin.id}'>{html.escape(admin.full_name)}</a>"
        )
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
        log_admin_action(
            group_id=group_id,
            admin_id=update.effective_user.id,
            admin_name=update.effective_user.full_name,
            action="unban",
            target_id=target_id,
        )
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been unbanned.", parse_mode="HTML")

        admin = update.effective_user
        await _post_to_log_channel(
            context, group_id,
            f"🔓 <b>Manual unban</b>\n"
            f"<b>User ID:</b> <a href='tg://user?id={target_id}'><code>{target_id}</code></a>\n"
            f"<b>By:</b> <a href='tg://user?id={admin.id}'>{html.escape(admin.full_name)}</a>"
        )
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

    # Route detections to the group's own configured log channel, not just the
    # global one — otherwise a group with a per-group channel gets its manual
    # sweep results (and action buttons) posted to the wrong place.
    log_channel  = _resolve_log_channel(group_id, context)
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
        result = await sweep_group(
            pyro, context.bot, group_id, log_channel, progress_cb=progress, trigger="manual"
        )
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

    # Mirror auto-sweep behavior: post the summary to the group's log channel
    # so audit-watchers see manual and automatic sweeps side by side.
    admin = update.effective_user
    await _post_to_log_channel(
        context, group_id,
        f"🧹 <b>Manual sweep complete</b>\n"
        f"<b>Triggered by:</b> <a href='tg://user?id={admin.id}'>{html.escape(admin.full_name)}</a>\n"
        f"Members seen: <code>{iterated}</code>\n"
        f"Checked: <code>{checked}</code>\n"
        f"Flagged: <code>{flagged}</code>\n"
        f"Errors: <code>{errors}</code>"
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

# ── Inline pagination helper (shared by /listwhitelist and /logs) ─────────────

_PAGE_SIZE = 15


def _paginate(lines: list[str], header: str, page: int, prefix: str, group_id: int):
    """
    Render one page of `lines` beneath `header`, with ◀/▶ nav buttons when
    there's more than one page. Returns (text, InlineKeyboardMarkup|None).
    Nav callbacks carry `prefix|group_id|page` so they're fully stateless —
    the handler just re-queries and re-renders for the requested page.
    """
    total = max(1, (len(lines) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total - 1))
    chunk = lines[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]
    page_note = f"\n<i>Page {page + 1}/{total}</i>" if total > 1 else ""
    text = f"{header}{page_note}\n\n" + "\n".join(chunk)

    markup = None
    if total > 1:
        row = []
        if page > 0:
            row.append(InlineKeyboardButton("◀ Prev", callback_data=f"{prefix}|{group_id}|{page - 1}"))
        if page < total - 1:
            row.append(InlineKeyboardButton("Next ▶", callback_data=f"{prefix}|{group_id}|{page + 1}"))
        markup = InlineKeyboardMarkup([row])
    return text, markup


def _build_whitelist_view(group_id: int) -> tuple[str, list[str], list[dict]]:
    """Return (header, flat sorted lines, raw rows) for the whitelist view.
    Lines are role-tagged (👑 admin / 🤖 bot / ✋ manual / 🛡 protected) and
    grouped by role so pagination stays readable without section headers."""
    rows = get_whitelist(group_id)
    admins, bots, manual, protected = [], [], [], []
    for r in rows:
        if r.get("is_bot"):
            bots.append(r)
        elif r.get("user_type") == "admin":
            admins.append(r)
        elif r.get("user_type") == "protected":
            protected.append(r)
        else:
            manual.append(r)

    def _fmt(tag: str, r: dict) -> str:
        name = html.escape(f"{r['first_name']} {r['last_name'] or ''}".strip())
        uname = f"@{html.escape(r['username'])}" if r['username'] else "no username"
        # Protected identities have synthetic negative ids — not clickable
        if r["user_id"] < 0:
            return f"{tag} {name} ({uname})"
        return f"{tag} <a href='tg://user?id={r['user_id']}'>{name}</a> ({uname})"

    lines = (
        [_fmt("👑", r) for r in admins]
        + [_fmt("🤖", r) for r in bots]
        + [_fmt("✋", r) for r in manual]
        + [_fmt("🛡", r) for r in protected]
    )
    header = (
        f"🛡 <b>Protected — {len(rows)} total</b>\n"
        f"<i>{len(admins)} admins · {len(bots)} bots · "
        f"{len(manual)} manual · {len(protected)} protected</i>"
    )
    return header, lines, rows


async def list_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Show protected users (role-tagged, paginated with ◀/▶) and attach a CSV
    export. (Folds in the old /exportwhitelist.)
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    header, lines, rows = _build_whitelist_view(group_id)
    if not rows:
        await update.message.reply_text("No protected users yet. Run /import_admins first.")
        return

    text, markup = _paginate(lines, header, 0, "wl_pg", group_id)
    await update.message.reply_text(
        text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup
    )

    # CSV export — attached to the initial reply only (page nav just edits text).
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


async def handle_whitelist_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nav callback for /listwhitelist pages. Callback: wl_pg|<group_id>|<page>."""
    query = update.callback_query
    await query.answer()
    try:
        _, gid, page = query.data.split("|")
        group_id, page = int(gid), int(page)
    except (ValueError, IndexError):
        return
    # group_id is from (forgeable) callback data — confirm the presser admins it
    # before rendering another group's whitelist (names, usernames, IDs).
    if not await _is_admin_of_group(context, group_id, query.from_user.id):
        await query.answer("Only admins of that group can view this.", show_alert=True)
        return
    header, lines, _ = _build_whitelist_view(group_id)
    text, markup = _paginate(lines, header, page, "wl_pg", group_id)
    try:
        await query.edit_message_text(
            text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup
        )
    except Exception:
        pass


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
        # Restrict to groups the caller actually administers — otherwise any
        # single-group admin would see every tenant's titles and counts.
        uid = update.effective_user.id
        scoped = []
        for g in all_gs:
            if await _is_admin_of_group(context, g["group_id"], uid):
                scoped.append(g)
        all_gs = scoped
        if not all_gs:
            await update.message.reply_text(
                "No groups found where you're an admin. Use /start to pick one."
            )
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

async def _resolve_alert(query, action_label: str) -> None:
    """
    Edit the detection-alert message in place: drop the buttons AND append a
    "Resolved" line naming the action taken and who took it. Falls back to
    just removing the buttons if the edit fails (e.g. message too old to edit,
    or message originated outside the bot).

    Called after every successful callback so the log channel keeps a
    permanent record of who pressed what — the original transient toast
    notification (query.answer) disappears once the admin closes Telegram.
    """
    admin = query.from_user
    admin_link = f"<a href='tg://user?id={admin.id}'>{html.escape(admin.full_name)}</a>"

    # text_html preserves the original HTML formatting (bold, links, etc.).
    # text_html_urled adds web previews; we don't want those.
    original = (query.message.text_html if query.message and query.message.text else "") or ""

    new_text = (
        f"{original}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{action_label} by {admin_link}"
    )

    try:
        await query.edit_message_text(
            new_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=None,
        )
    except Exception as e:
        # Common failure: "Message is not modified" or "Message to edit not found".
        # Strip the buttons at minimum so the admin sees their press registered.
        logger.warning(f"Could not edit alert text (falling back to button removal): {e}")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


async def handle_detection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles inline button presses on log-channel detection alerts.

    Callback data format: "<action>|<group_id>|<user_id>"
      unban_wl  — unban the user and add them to the whitelist
      unban_fp  — unban with a 30-day false-positive grace
      dismiss   — remove the buttons without taking action
      ban_now   — escalate an alert-mode detection to a permanent ban
      kick_now  — escalate an alert-mode detection to a kick (no ban)

    On success, every branch edits the original alert to remove the buttons
    AND appends a resolution line so the audit is permanently visible.
    On failure, the buttons stay so the admin can retry.
    """
    query = update.callback_query

    # Parse "<action>|<group_id>|<user_id>" defensively — this is untrusted
    # data that can be forged/replayed by any client.
    parts = (query.data or "").split("|")
    if len(parts) != 3:
        await query.answer("Malformed action.", show_alert=True)
        return
    try:
        action, group_id, user_id = parts[0], int(parts[1]), int(parts[2])
    except ValueError:
        await query.answer("Malformed action.", show_alert=True)
        return

    # AUTHORIZATION: these buttons ban/unban/whitelist and edit the cross-group
    # blocklist. They live in a log channel that may contain non-admins, and
    # group_id comes from the (forgeable) payload — so we MUST confirm the
    # presser is actually an admin of *that* group before doing anything.
    if not await _is_admin_of_group(context, group_id, query.from_user.id):
        await query.answer("Only admins of that group can do this.", show_alert=True)
        return

    admin_name = query.from_user.full_name

    if action == "dismiss":
        log_admin_action(
            group_id=group_id,
            admin_id=query.from_user.id,
            admin_name=admin_name,
            action="dismiss_alert",
            target_id=user_id,
            details="from alert button",
        )
        await _resolve_alert(query, "🗑 <b>Dismissed</b>")
        await query.answer("Dismissed.")
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
        # Record in the cross-group blocklist (human-confirmed escalation)
        # entry was already fetched above for insert_log
        add_known_bad_actor(
            user_id=user_id,
            username=(entry or {}).get("username"),
            full_name=(entry or {}).get("full_name"),
            reason="alert escalation",
            confirmed_by=query.from_user.id,
            source_group_id=group_id,
        )
        label = "🚫 <b>Banned</b>" if verb == "banned" else "👢 <b>Kicked</b>"
        await _resolve_alert(query, label)
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

        # Clear from cross-group blocklist — false-positive reversal
        remove_known_bad_actor(user_id)

        # Audit-log the action so /logs reflects who reversed what
        log_admin_action(
            group_id=group_id,
            admin_id=query.from_user.id,
            admin_name=admin_name,
            action="unban_whitelist" if was_banned else "whitelist",
            target_id=user_id,
            details="from alert button",
        )

        label = "✅ <b>Unbanned + Whitelisted</b>" if was_banned else "✅ <b>Whitelisted</b>"
        await _resolve_alert(query, label)
        msg = (
            f"Unbanned + whitelisted by {admin_name}." if was_banned
            else f"Whitelisted by {admin_name} (alert cleared)."
        )
        await query.answer(msg, show_alert=False)
        return

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
        # Clear from cross-group blocklist — false-positive reversal
        remove_known_bad_actor(user_id)
        log_admin_action(
            group_id=group_id,
            admin_id=query.from_user.id,
            admin_name=admin_name,
            action="unban_grace" if was_banned else "false_positive_grace",
            target_id=user_id,
            details="30-day grace, from alert button",
        )
        label = (
            "🔓 <b>Unbanned</b> (30-day grace)" if was_banned
            else "🔕 <b>Ignored</b> (30-day grace)"
        )
        await _resolve_alert(query, label)
        msg = (
            f"Unbanned (30-day grace) by {admin_name}." if was_banned
            else f"Alert ignored — 30-day grace set by {admin_name}."
        )
        await query.answer(msg, show_alert=False)
        return


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

    if added:
        # Audit the change — detection behaviour is being modified.
        log_admin_action(
            group_id=group_id,
            admin_id=update.effective_user.id,
            admin_name=update.effective_user.full_name,
            action="add_keyword",
            details=", ".join(e for e in entries)[:500],
        )

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
    esc = html.escape(pattern)
    if removed:
        log_admin_action(
            group_id=group_id,
            admin_id=update.effective_user.id,
            admin_name=update.effective_user.full_name,
            action="remove_keyword",
            details=pattern[:500],
        )
        await update.message.reply_text(f"✅ Keyword <code>{esc}</code> removed.", parse_mode="HTML")
    else:
        await update.message.reply_text(
            f"⚠️ <code>{esc}</code> not found in keyword list.", parse_mode="HTML"
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
        lines.append(f"• <code>{html.escape(r['pattern'])}</code> <i>({tag})</i>")

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

def _logs_user_link(uid: int | None, name: str | None, username: str | None) -> str:
    """Clickable user with @handle inline; graceful when uid/username missing."""
    display = html.escape(name or (f"@{username}" if username else f"ID {uid}" if uid else "?"))
    handle  = f" (@{html.escape(username)})" if username else ""
    if uid and uid > 0:
        return f"<a href='tg://user?id={uid}'>{display}</a>{handle}"
    return f"{display}{handle}"


def _build_logs_view(group_id: int, limit: int = 50) -> tuple[str, list[str]]:
    """Return (header, flat lines) merging recent detections (🚨) and admin
    actions (🔧), newest first within each, ready for pagination."""
    detections = get_recent_logs(group_id, limit)
    actions    = get_recent_admin_actions(group_id, limit)
    lines: list[str] = []

    for r in detections:
        dt       = r["created_at"].strftime("%m-%d %H:%M") if r["created_at"] else "?"
        imp_link = _logs_user_link(r["user_id"], r["full_name"], r["username"])
        dtype    = r["detection_type"] or "?"
        action   = r["action_taken"] or "?"
        if dtype == "keyword":
            details = r.get("details") or ""
            pattern = details[len("Matched: "):] if details.startswith("Matched: ") else details
            tgt_display = f"keyword <code>{html.escape(pattern)}</code>" if pattern else "keyword"
        else:
            tgt_display = _logs_user_link(
                r["target_user_id"], r["target_name"], r.get("target_username")
            )
        lines.append(f"🚨 <b>{dt}</b> — {imp_link} → {tgt_display} | <i>{dtype}</i> | {action}")

    for r in actions:
        dt = r["created_at"].strftime("%m-%d %H:%M") if r["created_at"] else "?"
        admin_display = html.escape(r["admin_name"] or f"ID {r['admin_id']}")
        who = f"<a href='tg://user?id={r['admin_id']}'>{admin_display}</a>"
        tgt = (
            f" → <a href='tg://user?id={r['target_id']}'><code>{r['target_id']}</code></a>"
            if r["target_id"] else ""
        )
        detail = f" ({html.escape(r['details'])})" if r["details"] else ""
        lines.append(f"🔧 <b>{dt}</b> {who} — {r['action']}{tgt}{detail}")

    header = "📋 <b>Recent activity</b> — 🚨 detections + 🔧 admin actions"
    return header, lines


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Recent activity for the group — detections AND admin actions, paginated
    with ◀/▶. Usage: /logs (optional /logs <N> caps how many of each are fetched).
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, _ = ctx

    limit = 50
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 100))
        except ValueError:
            pass

    header, lines = _build_logs_view(group_id, limit)
    if not lines:
        await update.message.reply_text(
            "No activity logged for this group yet.\n"
            "Detections appear here when an impersonator is caught; admin actions "
            "(whitelist / ban / setaction etc.) are recorded too."
        )
        return

    text, markup = _paginate(lines, header, 0, "logs_pg", group_id)
    await update.message.reply_text(
        text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup
    )


async def handle_logs_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nav callback for /logs pages. Callback: logs_pg|<group_id>|<page>."""
    query = update.callback_query
    await query.answer()
    try:
        _, gid, page = query.data.split("|")
        group_id, page = int(gid), int(page)
    except (ValueError, IndexError):
        return
    # group_id is from (forgeable) callback data — confirm the presser admins it
    # before rendering another group's detection/admin logs.
    if not await _is_admin_of_group(context, group_id, query.from_user.id):
        await query.answer("Only admins of that group can view this.", show_alert=True)
        return
    header, lines = _build_logs_view(group_id)
    text, markup = _paginate(lines, header, page, "logs_pg", group_id)
    try:
        await query.edit_message_text(
            text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup
        )
    except Exception:
        pass


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
        f"✅ Imported <b>{added}</b> user(s) into whitelist for <b>{html.escape(str(group_title))}</b>."
        + (f"\n⚠️ Skipped {skipped} invalid row(s)." if skipped else ""),
        parse_mode="HTML",
    )
    if failed_rows:
        # failed_rows embed arbitrary CSV cell values (bad_val!r) — escape.
        detail = "Failed rows:\n" + "\n".join(failed_rows[:20])
        if len(failed_rows) > 20:
            detail += f"\n…and {len(failed_rows) - 20} more."
        await update.message.reply_text(f"<pre>{html.escape(detail)}</pre>", parse_mode="HTML")


# ── /settings ─────────────────────────────────────────────────────────────────

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a single overview of all per-group settings."""
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx

    group = get_group(group_id) or {}

    action_mode = group.get("action_mode") or "ban"

    # Global similarity threshold (legacy single value)
    sim_thr = group.get("similarity_threshold")
    sim_label = f"<code>{sim_thr}</code>" if sim_thr is not None else f"<code>{NAME_SIMILARITY_THRESHOLD}</code> (default)"

    # Per-type thresholds
    uname_thr = group.get("username_threshold")
    name_thr  = group.get("name_threshold")
    uname_label = f"<code>{uname_thr}</code>" if uname_thr is not None else f"<code>{USERNAME_SIMILARITY_THRESHOLD}</code> (default)"
    name_label  = f"<code>{name_thr}</code>"  if name_thr  is not None else f"<code>{NAME_SIMILARITY_THRESHOLD}</code> (default)"

    # Score bands
    ban_score   = group.get("ban_score")
    alert_score = group.get("alert_score")
    ban_label   = f"<code>{ban_score}</code>"   if ban_score   is not None else f"<code>{DEFAULT_BAN_SCORE}</code> (default)"
    alert_label = f"<code>{alert_score}</code>" if alert_score is not None else f"<code>{DEFAULT_ALERT_SCORE}</code> (default)"

    # Cross-group blocklist
    blocklist_on = bool(group.get("use_global_blocklist", True))
    blocklist_label = "on" if blocklist_on else "off"

    # Log channel
    log_ch = group.get("log_channel_id")
    log_label = f"<code>{log_ch}</code>" if log_ch else "not set — using global"

    # Counts
    wl_rows  = get_whitelist(group_id)
    kw_rows  = get_reserved_keywords(group_id)
    wl_count = len(wl_rows)
    kw_count = len(kw_rows)

    # Pyrogram availability
    pyro_ok = bool(context.bot_data.get("pyro_client"))
    pyro_label = "available" if pyro_ok else "not configured"

    await update.message.reply_text(
        f"⚙️ <b>Settings — {html.escape(group_title)}</b>\n\n"
        f"<b>Action mode:</b> <code>{html.escape(action_mode)}</code>\n\n"
        f"<b>Similarity thresholds</b>\n"
        f"  Global:   {sim_label}\n"
        f"  Username: {uname_label}\n"
        f"  Name:     {name_label}\n\n"
        f"<b>Score bands</b>\n"
        f"  Ban score:   {ban_label}\n"
        f"  Alert score: {alert_label}\n\n"
        f"<b>Cross-group blocklist:</b> {blocklist_label}\n"
        f"<b>Log channel:</b> {log_label}\n\n"
        f"<b>Protected users:</b> <code>{wl_count}</code>\n"
        f"<b>Reserved keywords:</b> <code>{kw_count}</code>\n\n"
        f"<b>Pyrogram watcher:</b> {pyro_label}",
        parse_mode="HTML",
    )


# ── /setbands ─────────────────────────────────────────────────────────────────

async def set_bands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setbands <ban_score> <alert_score>

    Set the composite-score thresholds used to decide whether a detection
    triggers a ban or merely an alert.  Both values must be integers 50–100
    and ban_score must be >= alert_score.
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /setbands &lt;ban_score&gt; &lt;alert_score&gt;\n\n"
            "Both values must be integers between 50 and 100, "
            "and ban_score must be ≥ alert_score.\n\n"
            f"Defaults: ban <code>{DEFAULT_BAN_SCORE}</code>, "
            f"alert <code>{DEFAULT_ALERT_SCORE}</code>.",
            parse_mode="HTML",
        )
        return

    try:
        ban_val   = int(context.args[0])
        alert_val = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Both values must be integers.")
        return

    errors = []
    if not (50 <= ban_val <= 100):
        errors.append("ban_score must be between 50 and 100")
    if not (50 <= alert_val <= 100):
        errors.append("alert_score must be between 50 and 100")
    if ban_val < alert_val:
        errors.append("ban_score must be ≥ alert_score")

    if errors:
        await update.message.reply_text(
            "❌ " + "; ".join(errors) + ".",
            parse_mode="HTML",
        )
        return

    upsert_group(group_id, title=group_title)
    set_group_score_bands(group_id, ban_val, alert_val)
    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="setbands",
        details=f"ban={ban_val} alert={alert_val}",
    )
    await update.message.reply_text(
        f"✅ Score bands updated — ban: <code>{ban_val}</code>, "
        f"alert: <code>{alert_val}</code>.",
        parse_mode="HTML",
    )


# ── /setthresholds ────────────────────────────────────────────────────────────

async def set_type_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setthresholds username=88 name=85

    Set per-type fuzzy-match thresholds.  Supply one or both key=value tokens.
    Valid range: 50–100.  Omitted keys are left unchanged.
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx

    if not context.args:
        await update.message.reply_text(
            "Usage: /setthresholds username=&lt;n&gt; name=&lt;n&gt;\n\n"
            "Provide one or both key=value pairs. Values must be 50–100.\n\n"
            f"Defaults: username <code>{USERNAME_SIMILARITY_THRESHOLD}</code>, "
            f"name <code>{NAME_SIMILARITY_THRESHOLD}</code>.",
            parse_mode="HTML",
        )
        return

    username_threshold = None
    name_threshold     = None
    errors = []

    for token in context.args:
        if "=" not in token:
            errors.append(f"<code>{html.escape(token)}</code> — expected key=value format")
            continue
        key, _, raw_val = token.partition("=")
        key = key.strip().lower()
        try:
            val = int(raw_val.strip())
        except ValueError:
            errors.append(f"<code>{html.escape(token)}</code> — value must be an integer")
            continue
        if not (50 <= val <= 100):
            errors.append(f"<code>{html.escape(token)}</code> — value must be 50–100")
            continue
        if key == "username":
            username_threshold = val
        elif key in ("name", "display_name", "displayname"):
            name_threshold = val
        else:
            errors.append(f"<code>{html.escape(token)}</code> — unknown key (use username or name)")

    if errors:
        await update.message.reply_text(
            "⚠️ Some tokens were invalid:\n" + "\n".join(f"  • {e}" for e in errors),
            parse_mode="HTML",
        )
        if username_threshold is None and name_threshold is None:
            return

    if username_threshold is None and name_threshold is None:
        await update.message.reply_text(
            "No valid thresholds provided. "
            "Use: /setthresholds username=88 name=85",
            parse_mode="HTML",
        )
        return

    upsert_group(group_id, title=group_title)
    set_group_thresholds(
        group_id,
        username_threshold=username_threshold,
        name_threshold=name_threshold,
    )

    set_parts = []
    if username_threshold is not None:
        set_parts.append(f"username: <code>{username_threshold}</code>")
    if name_threshold is not None:
        set_parts.append(f"name: <code>{name_threshold}</code>")

    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="setthresholds",
        details=", ".join(
            ([f"username={username_threshold}"] if username_threshold is not None else [])
            + ([f"name={name_threshold}"] if name_threshold is not None else [])
        ),
    )
    await update.message.reply_text(
        "✅ Thresholds updated — " + ", ".join(set_parts) + ".",
        parse_mode="HTML",
    )


# ── /blocklist ─────────────────────────────────────────────────────────────────

async def blocklist_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /blocklist on|off

    Toggle this group's participation in the cross-group blocklist.
    When enabled, any user that has been manually confirmed-banned in ANY
    managed group is automatically actioned here too.
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx

    ON_VALS  = {"on", "enable", "enabled", "true", "1", "yes"}
    OFF_VALS = {"off", "disable", "disabled", "false", "0", "no"}

    if not context.args or context.args[0].lower() not in ON_VALS | OFF_VALS:
        await update.message.reply_text(
            "Usage: /blocklist on|off\n\n"
            "<b>Cross-group blocklist:</b> when a user is manually confirmed-banned "
            "in any managed group (via /ban or the Ban button on an alert), they are "
            "added to a shared blocklist. Any other managed group with this setting "
            "enabled will automatically action them when they join.\n\n"
            "Current state: use /settings to check.",
            parse_mode="HTML",
        )
        return

    enabled = context.args[0].lower() in ON_VALS
    upsert_group(group_id, title=group_title)
    set_group_blocklist(group_id, enabled)
    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="blocklist",
        details="on" if enabled else "off",
    )
    state_word = "enabled" if enabled else "disabled"
    note = (
        "Users confirmed-banned in other managed groups will be automatically "
        "actioned when they join this group."
        if enabled else
        "This group will not receive automatic actions from the shared blocklist."
    )
    await update.message.reply_text(
        f"✅ Cross-group blocklist <b>{state_word}</b> for "
        f"<b>{html.escape(group_title)}</b>.\n\n{note}",
        parse_mode="HTML",
    )


# ── /protect ──────────────────────────────────────────────────────────────────

async def protect_identity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Protect an external identity (person without a Telegram account).

    Usage:
      /protect Full Name
      (reply to a photo message) /protect Full Name

    Creates a whitelist row with a stable synthetic ID so the detection
    engine catches impersonators of this name (and photo if provided).
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/protect Full Name</code>\n\n"
            "Optionally, reply to a photo message to also hash their photo.\n"
            "Any future member whose name (or photo) closely matches this "
            "identity will be flagged as a potential impersonator.",
            parse_mode="HTML",
        )
        return

    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Please provide a non-empty name.")
        return

    # Stable synthetic negative ID — guaranteed no collision with real (positive)
    # Telegram IDs. MUST be deterministic across restarts so re-running /protect
    # for the same name UPDATES the row instead of creating duplicates; Python's
    # built-in hash() is per-process randomized, so use a fixed digest instead.
    import hashlib
    _digest = hashlib.sha1(name.lower().strip().encode("utf-8")).hexdigest()
    synthetic_id = -(int(_digest[:12], 16) % 1_000_000_000_000)

    # Optionally hash the photo from a replied-to message
    pfp_hash = None
    reply_msg = update.message.reply_to_message
    if reply_msg and reply_msg.photo:
        try:
            f = await reply_msg.photo[-1].get_file()
            pfp_hash = compute_pfp_hash_bytes(bytes(await f.download_as_bytearray()))
        except Exception as e:
            logger.warning(f"Could not hash photo for /protect {name!r}: {e}")
            await update.message.reply_text(
                f"⚠️ Could not download the photo (<code>{html.escape(str(e))}</code>). "
                "Proceeding with name-only protection.",
                parse_mode="HTML",
            )

    upsert_whitelisted_user(
        group_id=group_id,
        user_id=synthetic_id,
        username=None,
        first_name=name,
        last_name=None,
        pfp_hash=pfp_hash,
        whitelisted_by=update.effective_user.id,
        user_type="protected",
        is_bot=False,
    )
    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="protect",
        details=name,
    )

    photo_note = " Photo hash stored for visual matching." if pfp_hash else ""
    await update.message.reply_text(
        f"✅ <b>{html.escape(name)}</b> is now protected.\n"
        f"Impersonators of this name will be detected.{photo_note}\n\n"
        f"<i>(Synthetic ID: <code>{synthetic_id}</code>)</i>",
        parse_mode="HTML",
    )


# ── /clearwhitelist undo callback ─────────────────────────────────────────────

async def handle_whitelist_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Callback handler for the '↩️ Undo clear' button posted by /clearwhitelist.

    Callback data format: "wl_undo|<group_id>"
    """
    query = update.callback_query

    parts = query.data.split("|")
    if len(parts) != 2:
        await query.answer("Invalid callback data.", show_alert=True)
        return

    try:
        group_id = int(parts[1])
    except ValueError:
        await query.answer("Invalid group ID.", show_alert=True)
        return

    # The confirmation (and this button) is posted in the group, so any member
    # could otherwise press it to restore the whole whitelist. Gate on admin.
    if not await _is_admin_of_group(context, group_id, query.from_user.id):
        await query.answer("Only admins of that group can undo this.", show_alert=True)
        return

    rows = _clearwhitelist_undo.get(group_id)
    if not rows:
        await query.answer(
            "Undo no longer available — restore from the CSV backup instead.",
            show_alert=True,
        )
        return

    await query.answer()

    restored = 0
    clicker_id = query.from_user.id
    for r in rows:
        try:
            upsert_whitelisted_user(
                group_id=group_id,
                user_id=r["user_id"],
                username=r.get("username"),
                first_name=r.get("first_name") or "Unknown",
                last_name=r.get("last_name"),
                pfp_hash=r.get("pfp_hash"),
                whitelisted_by=clicker_id,
                user_type=r.get("user_type") or "manual",
                is_bot=bool(r.get("is_bot", False)),
            )
            restored += 1
        except Exception as e:
            logger.warning(f"Undo whitelist: failed to restore user {r.get('user_id')}: {e}")

    # Discard the snapshot so a second press returns the "no longer available" message
    del _clearwhitelist_undo[group_id]

    log_admin_action(
        group_id=group_id,
        admin_id=clicker_id,
        admin_name=query.from_user.full_name,
        action="clearwhitelist_undo",
        details=f"Restored {restored} user(s)",
    )

    try:
        await query.edit_message_text(
            f"↩️ Whitelist restored — <b>{restored}</b> user(s) re-added.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Could not edit undo confirmation message: {e}")
        await query.answer(f"Restored {restored} user(s).", show_alert=True)


# ── /clearwhitelist ───────────────────────────────────────────────────────────

async def clear_whitelist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Remove ALL protected users from this group's whitelist.
    Requires /clearwhitelist confirm to execute (safety gate).

    Before deletion, posts a CSV backup of the current whitelist to the
    group's log channel — free disaster recovery: an admin who clears by
    mistake (or whose account is compromised) can repopulate via
    /importwhitelist on that file. Mirrors the format /listwhitelist
    emits so the round-trip works without translation.
    """
    ctx = await _get_admin_group(update, context)
    if not ctx:
        return
    group_id, group_title = ctx

    rows = get_whitelist(group_id)

    if not context.args or context.args[0].lower() != "confirm":
        await update.message.reply_text(
            f"⚠️ <b>This will remove all {len(rows)} protected user(s) from the whitelist.</b>\n\n"
            "The bot will no longer detect impersonators until you re-run "
            "/import_admins or add users back manually.\n\n"
            "Before wiping, a CSV backup will be posted to the log channel "
            "so you can /importwhitelist it back if needed.\n\n"
            "To proceed: /clearwhitelist confirm",
            parse_mode="HTML",
        )
        return

    # Pre-wipe backup — sent to the group's log channel (or the global fallback)
    if rows:
        log_channel = _resolve_log_channel(group_id, context)
        if log_channel:
            try:
                buf = io.StringIO()
                fieldnames = ["user_id", "username", "first_name", "last_name", "user_type", "is_bot", "created_at"]
                writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
                file_bytes = io.BytesIO(buf.getvalue().encode("utf-8"))
                from datetime import datetime, timezone
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                fname = f"whitelist-backup-{group_id}-{stamp}.csv"
                await context.bot.send_document(
                    chat_id=log_channel,
                    document=InputFile(file_bytes, filename=fname),
                    caption=(
                        f"💾 <b>Whitelist backup — pre-wipe</b>\n"
                        f"<b>Group:</b> {html.escape(group_title)} (<code>{group_id}</code>)\n"
                        f"<b>Rows:</b> <code>{len(rows)}</code>\n"
                        f"<b>Triggered by:</b> "
                        f"<a href='tg://user?id={update.effective_user.id}'>"
                        f"{html.escape(update.effective_user.full_name)}</a>\n\n"
                        "To restore: DM the bot and reply to this file with /importwhitelist."
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                # Don't block the wipe on a backup-post failure — admin asked
                # for it explicitly and we already warned them about the risk.
                logger.warning(f"Could not post pre-wipe backup for {group_id}: {e}")
                await update.message.reply_text(
                    "⚠️ Could not post backup to the log channel — proceeding with the wipe anyway. "
                    f"Error: <code>{html.escape(str(e))}</code>",
                    parse_mode="HTML",
                )

    # Snapshot rows BEFORE the wipe so the undo button can restore them
    _clearwhitelist_undo[group_id] = rows

    count = db_clear_whitelist(group_id)
    log_admin_action(
        group_id=group_id,
        admin_id=update.effective_user.id,
        admin_name=update.effective_user.full_name,
        action="clearwhitelist",
        details=f"Removed {count} user(s)",
    )
    undo_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Undo clear", callback_data=f"wl_undo|{group_id}"),
    ]])
    await update.message.reply_text(
        f"✅ Whitelist cleared — <b>{count}</b> user(s) removed.\n"
        "Backup posted to the log channel.\n"
        "Run /import_admins to repopulate.",
        parse_mode="HTML",
        reply_markup=undo_markup,
    )


