
import html
import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus, ChatType

from src.db import upsert_group, is_whitelisted, get_group, upsert_whitelisted_user, mark_seen
from src.utils.checker import UserSnapshot, check_user, ban_and_log
from src.utils.image import compute_pfp_hash_bytes
from src.config import LOG_CHANNEL_ID

logger = logging.getLogger(__name__)


async def on_bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fires when the bot's own membership status changes (MY_CHAT_MEMBER updates).
    Used to auto-register new groups the moment the bot is added.
    """
    my = update.my_chat_member
    if not my:
        return

    bot_id = context.bot.id
    if my.new_chat_member.user.id != bot_id:
        return

    new_status = my.new_chat_member.status
    chat = update.effective_chat

    # Only register actual groups/supergroups — not channels (which also fire
    # MY_CHAT_MEMBER when the bot is added as admin, causing duplicate /stats rows)
    if new_status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR] and \
            chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        # Fetch group PFP so the bot can detect impersonators of the group itself
        group_pfp_hash = None
        try:
            full_chat = await context.bot.get_chat(chat.id)
            if full_chat.photo:
                f = await context.bot.get_file(full_chat.photo.big_file_id)
                raw = bytes(await f.download_as_bytearray())
                from src.utils.image import compute_pfp_hash_bytes
                group_pfp_hash = compute_pfp_hash_bytes(raw)
        except Exception:
            pass
        upsert_group(chat.id, title=chat.title, pfp_hash=group_pfp_hash)
        logger.info(f"Bot added to group {chat.title} ({chat.id}) — registered.")

        log_channel = context.bot_data.get("log_channel_id") or LOG_CHANNEL_ID
        if log_channel:
            try:
                await context.bot.send_message(
                    chat_id=log_channel,
                    text=(
                        f"➕ <b>Bot added to new group</b>\n"
                        f"<b>{html.escape(chat.title or str(chat.id))}</b> (<code>{chat.id}</code>)\n\n"
                        f"Run /import_admins in that group to populate the whitelist."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass


async def check_impersonation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    new_member = result.new_chat_member
    old_member = result.old_chat_member
    user = new_member.user
    group_id = update.effective_chat.id

    # Auto-whitelist when a member is promoted to admin (handles ongoing admin changes
    # that /import_admins would miss since it's a one-time snapshot).
    if (
        new_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
        and old_member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
        and not user.is_bot
    ):
        upsert_group(group_id, title=update.effective_chat.title)
        pfp_hash = None
        try:
            photos = await user.get_profile_photos(limit=1)
            if photos.total_count > 0:
                f = await photos.photos[0][-1].get_file()
                pfp_hash = compute_pfp_hash_bytes(bytes(await f.download_as_bytearray()))
        except Exception:
            pass
        upsert_whitelisted_user(
            group_id=group_id,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            pfp_hash=pfp_hash,
            whitelisted_by=context.bot.id,
            user_type="admin",
        )
        mark_seen(group_id, user.id)
        logger.info(f"Auto-whitelisted promoted admin {user.full_name} ({user.id}) in group {group_id}.")
        return

    # Only continue for fresh joins (not kicks/unbans/demotions)
    if old_member.status not in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
        return
    if new_member.status not in [ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED]:
        return

    if user.is_bot:
        return

    # Auto-register group if not yet in DB
    upsert_group(group_id, title=update.effective_chat.title)

    if is_whitelisted(group_id, user.id):
        return

    # Capture the invite link used to join (None for public joins / admin adds)
    invite_link: str | None = None
    if update.chat_member.invite_link:
        invite_link = update.chat_member.invite_link.invite_link

    logger.info(f"New member {user.full_name} ({user.id}) in group {group_id} — running check.")

    # Fetch profile photo
    pfp_bytes = None
    try:
        photos = await user.get_profile_photos(limit=1)
        if photos.total_count > 0:
            photo_file = await photos.photos[0][-1].get_file()
            pfp_bytes = bytes(await photo_file.download_as_bytearray())
    except Exception as e:
        logger.warning(f"Could not fetch PFP for {user.id}: {e}")

    snapshot = UserSnapshot(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        pfp_bytes=pfp_bytes,
    )

    detection = await check_user(snapshot, group_id)
    if not detection.flagged:
        return

    # Guard against false positives: if the joining user is already an admin
    # (e.g. added directly), whitelist them silently instead of banning.
    try:
        member_info = await context.bot.get_chat_member(group_id, user.id)
        if member_info.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            upsert_whitelisted_user(
                group_id=group_id,
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                pfp_hash=compute_pfp_hash_bytes(pfp_bytes) if pfp_bytes else None,
                whitelisted_by=context.bot.id,
                user_type="admin",
            )
            mark_seen(group_id, user.id)
            logger.info(f"Auto-whitelisted admin {user.id} after false-positive on join in group {group_id}.")
            return
    except Exception:
        pass

    group = get_group(group_id)
    log_channel = (group["log_channel_id"] if group else None) or context.bot_data.get("log_channel_id") or LOG_CHANNEL_ID

    async def _ban(gid: int, uid: int):
        await context.bot.ban_chat_member(chat_id=gid, user_id=uid)

    log_notify = None
    if log_channel:
        async def log_notify(text: str, markup=None, _lc=log_channel):
            await context.bot.send_message(chat_id=_lc, text=text, parse_mode="HTML", reply_markup=markup)

    async def _unban(gid: int, uid: int):
        await context.bot.unban_chat_member(chat_id=gid, user_id=uid)

    await ban_and_log(
        result=detection,
        snapshot=snapshot,
        group_id=group_id,
        trigger="join",
        ban_func=_ban,
        unban_func=_unban,
        log_channel_notify=log_notify,
        invite_link=invite_link,
    )
