
"""
Message-based impersonation scanning (RELAXED — the only mode).

Each user is checked once per group, the first time they send a message,
then their `seen_members` row prevents re-checking. Profile changes after
that point are caught in real time by the Pyrogram watcher
(`src/watcher/events.py`) and by the periodic 6-hour sweep
(`src/watcher/sweep.py`) — no need to re-scan every message.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus, ChatType

from src.db import get_group, is_whitelisted, is_seen, mark_seen, upsert_whitelisted_user
from src.utils.checker import UserSnapshot, check_user, ban_and_log
from src.utils.image import compute_pfp_hash_bytes
from src.config import LOG_CHANNEL_ID

logger = logging.getLogger(__name__)


async def scan_message_sender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_user:
        return
    if update.effective_chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    user = update.effective_user
    if user.is_bot:
        return

    group_id = update.effective_chat.id
    group    = get_group(group_id)
    if not group:
        # Bot not yet registered for this group — skip until /import_admins is run
        return

    if is_whitelisted(group_id, user.id):
        return

    # Skip permanently once the user has been checked
    if is_seen(group_id, user.id):
        return

    # Fetch PFP for the detection pipeline
    pfp_bytes = None
    try:
        photos = await user.get_profile_photos(limit=1)
        if photos.total_count > 0:
            photo_file = await photos.photos[0][-1].get_file()
            pfp_bytes = bytes(await photo_file.download_as_bytearray())
    except Exception as e:
        logger.debug(f"Could not fetch PFP for {user.id}: {e}")

    snapshot = UserSnapshot(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        pfp_bytes=pfp_bytes,
    )

    detection = await check_user(snapshot, group_id)
    mark_seen(group_id, user.id)

    if not detection.flagged:
        return

    # Guard against false positives on first setup: if the flagged user is
    # actually a current group admin, whitelist them silently instead of banning.
    try:
        member_info = await context.bot.get_chat_member(group_id, user.id)
        if member_info.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            upsert_whitelisted_user(
                group_id=group_id,
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                pfp_hash=compute_pfp_hash_bytes(pfp_bytes) if pfp_bytes else None,
                whitelisted_by=context.bot.id,
                user_type="admin",
                is_bot=bool(user.is_bot),
            )
            logger.info(
                f"Auto-whitelisted admin {user.id} after false-positive detection in group {group_id}."
            )
            return
    except Exception:
        pass

    log_channel = (
        (group["log_channel_id"] if group else None)
        or context.bot_data.get("log_channel_id")
        or LOG_CHANNEL_ID
    )

    async def _ban(gid: int, uid: int):
        await context.bot.ban_chat_member(chat_id=gid, user_id=uid)

    async def _unban(gid: int, uid: int):
        await context.bot.unban_chat_member(chat_id=gid, user_id=uid)

    log_notify = None
    if log_channel:
        async def log_notify(text: str, markup=None, _lc=log_channel):
            await context.bot.send_message(
                chat_id=_lc, text=text, parse_mode="HTML", reply_markup=markup
            )

    await ban_and_log(
        result=detection,
        snapshot=snapshot,
        group_id=group_id,
        trigger="message",
        ban_func=_ban,
        unban_func=_unban,
        log_channel_notify=log_notify,
    )
