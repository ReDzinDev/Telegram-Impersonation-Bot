
"""
Message-based impersonation scanning.

RELAXED (default) — checks a user only the first time they send a message
  in a group. After that they are marked as "seen" and skipped.

STRICT — re-checks every message sender, but no more than once per
  STRICT_RECHECK_INTERVAL seconds (in-memory TTL cache). This prevents
  hammering the DB and Telegram API for active chatters while still
  catching post-join profile changes.
"""
import logging
import time
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatType

from src.db import get_group, is_whitelisted, is_seen, mark_seen

# In-memory cache: (group_id, user_id) -> last_checked unix timestamp
# Only consulted in STRICT mode; RELAXED uses the persistent seen_members table.
_strict_cache: dict[tuple[int, int], float] = {}
STRICT_RECHECK_INTERVAL = 300  # seconds (5 minutes)
from src.utils.checker import UserSnapshot, check_user, ban_and_log
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

    group = get_group(group_id)
    if not group:
        # Bot not yet registered for this group — skip until /import_admins is run
        return

    if is_whitelisted(group_id, user.id):
        return
    mode = group["check_mode"] if group else "relaxed"

    key = (group_id, user.id)

    if mode == "relaxed":
        # Skip permanently once the user has been checked
        if is_seen(group_id, user.id):
            return
    else:
        # STRICT: skip if checked within the TTL window
        now = time.time()
        last = _strict_cache.get(key, 0)
        if now - last < STRICT_RECHECK_INTERVAL:
            return
        # Prune stale entries to prevent unbounded memory growth
        if len(_strict_cache) > 10_000:
            cutoff = now - STRICT_RECHECK_INTERVAL
            stale = [k for k, v in _strict_cache.items() if v < cutoff]
            for k in stale:
                del _strict_cache[k]

    # Fetch PFP
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

    # Update both the persistent seen table (RELAXED) and the in-memory TTL (STRICT)
    mark_seen(group_id, user.id)
    _strict_cache[key] = time.time()

    if not detection.flagged:
        return

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
        trigger="message",
        ban_func=_ban,
        notify_func=_notify,
        log_channel_notify=log_notify,
    )
