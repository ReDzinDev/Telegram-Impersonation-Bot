
import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus

from src.db import upsert_group, is_whitelisted, get_group
from src.utils.checker import UserSnapshot, check_user, ban_and_log
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

    if new_status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR]:
        upsert_group(chat.id, title=chat.title)
        logger.info(f"Bot added to group {chat.title} ({chat.id}) — registered.")

        log_channel = context.bot_data.get("log_channel_id") or LOG_CHANNEL_ID
        if log_channel:
            try:
                await context.bot.send_message(
                    chat_id=log_channel,
                    text=(
                        f"➕ <b>Bot added to new group</b>\n"
                        f"<b>{chat.title}</b> (<code>{chat.id}</code>)\n\n"
                        f"Run /import_admins in that group to populate the whitelist."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass


async def check_impersonation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    new_member = result.new_chat_member

    # Only trigger when someone transitions from outside → member/restricted
    if result.old_chat_member.status not in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
        return
    if new_member.status not in [ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED]:
        return

    user = new_member.user
    if user.is_bot:
        return

    group_id = update.effective_chat.id
    group_title = update.effective_chat.title

    # Auto-register group if not yet in DB
    upsert_group(group_id, title=group_title)

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

    group = get_group(group_id)
    log_channel = (group["log_channel_id"] if group else None) or context.bot_data.get("log_channel_id") or LOG_CHANNEL_ID

    async def _ban(gid: int, uid: int):
        await context.bot.ban_chat_member(chat_id=gid, user_id=uid)

    async def _notify(gid: int, text: str):
        await context.bot.send_message(chat_id=gid, text=text, parse_mode="HTML")

    log_notify = None
    if log_channel:
        async def log_notify(text: str):
            await context.bot.send_message(chat_id=log_channel, text=text, parse_mode="HTML")

    await ban_and_log(
        result=detection,
        snapshot=snapshot,
        group_id=group_id,
        trigger="join",
        ban_func=_ban,
        notify_func=_notify,
        log_channel_notify=log_notify,
        invite_link=invite_link,
    )
