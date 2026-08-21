
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

from src.db import (
    get_group, is_whitelisted, is_seen, mark_seen, upsert_whitelisted_user,
    DatabaseUnavailable, run_db,
)
from src.utils.checker import UserSnapshot, check_user, ban_and_log
from src.utils.image import compute_pfp_hash_bytes
from src.config import LOG_CHANNEL_ID

logger = logging.getLogger(__name__)


def _scan_gate(group_id: int, user_id: int):
    """
    Decide whether this sender needs scanning, in one trip to the database.

    Returns the group config row to proceed with, or None to skip. Runs in a
    worker thread via run_db — never call it from the event loop directly.

    Fails closed on DatabaseUnavailable: skipping a scan during an outage is
    recoverable, acting on a half-known protection state is not.
    """
    try:
        group = get_group(group_id)
        if not group:
            # Not registered yet — skip until /import_admins has been run.
            return None
        if is_whitelisted(group_id, user_id):
            return None
        if is_seen(group_id, user_id):     # already checked once; permanent skip
            return None
        return group
    except DatabaseUnavailable as e:
        logger.warning(f"Skipping message scan in {group_id}: {e}")
        return None


async def scan_message_sender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message or not update.effective_user:
        return
    if update.effective_chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    user = update.effective_user
    if user.is_bot:
        return

    group_id = update.effective_chat.id

    # Three blocking reads used to run inline here, for EVERY message in every
    # monitored group. Collapsed into a single hop off the event loop.
    group = await run_db(_scan_gate, group_id, user.id)
    if group is None:
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
    await run_db(mark_seen, group_id, user.id)

    if not detection.flagged:
        return

    # Guard against false positives on first setup: if the flagged user is
    # actually a current group admin, whitelist them silently instead of banning.
    try:
        member_info = await context.bot.get_chat_member(group_id, user.id)
        if member_info.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await run_db(
                upsert_whitelisted_user,
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

    from src.utils.checker import make_action_funcs
    ban_func, unban_func, log_notify = make_action_funcs(context.bot, log_channel)

    await ban_and_log(
        result=detection,
        snapshot=snapshot,
        group_id=group_id,
        trigger="message",
        ban_func=ban_func,
        unban_func=unban_func,
        log_channel_notify=log_notify,
    )
