
"""
Shared impersonation check and ban logic.
Called from: join handler, message handler, sweep, and Pyrogram profile-change watcher.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable

from telegram import InlineKeyboardMarkup, InlineKeyboardButton

from src.db import (
    get_whitelist, is_whitelisted, insert_log, get_group,
    mark_seen, unmark_seen,
)
from src.utils.detector import check_username_similarity, check_name_similarity, check_homoglyph_danger
from src.utils.image import compute_pfp_hash_bytes, check_pfp_similarity
from src.config import NAME_SIMILARITY_THRESHOLD, PFP_HASH_THRESHOLD, LOG_CHANNEL_ID

logger = logging.getLogger(__name__)

# Telegram sentinel accounts that post on behalf of groups/channels.
# Both have is_bot=True but we guard here as belt-and-suspenders for any
# path (e.g. raw Pyrogram updates) that may not have checked is_bot first.
_SKIP_USER_IDS: frozenset[int] = frozenset({
    1087968824,  # GroupAnonymousBot  — anonymous admin messages
    136817688,   # Channel Bot        — linked-channel posts in groups
})


@dataclass
class UserSnapshot:
    """Normalised user data passed into the checker from any source."""
    user_id: int
    username: Optional[str]
    first_name: str
    last_name: Optional[str]
    pfp_bytes: Optional[bytes] = None   # raw bytes of current profile photo


@dataclass
class DetectionResult:
    flagged: bool
    match_type: Optional[str] = None     # 'username' | 'name' | 'pfp' | 'homoglyph_username' | 'homoglyph_name'
    matched_val: Optional[str] = None
    score: float = 0.0
    target_user_id: Optional[int] = None
    target_name: Optional[str] = None


async def check_user(
    snapshot: UserSnapshot,
    group_id: int,
) -> DetectionResult:
    """
    Run all impersonation checks for a user against the group's whitelist.
    Returns a DetectionResult — does NOT ban; callers decide what to do.
    """
    if snapshot.user_id in _SKIP_USER_IDS:
        return DetectionResult(flagged=False)

    if is_whitelisted(group_id, snapshot.user_id):
        return DetectionResult(flagged=False)

    whitelist = get_whitelist(group_id)
    if not whitelist:
        return DetectionResult(flagged=False)

    usernames  = [w["username"] for w in whitelist if w["username"]]
    names      = [f"{w['first_name']} {w['last_name'] or ''}".strip() for w in whitelist]
    pfp_hashes = [w["pfp_hash"] for w in whitelist if w["pfp_hash"]]

    full_name = f"{snapshot.first_name} {snapshot.last_name or ''}".strip()

    # 1 — Username similarity
    if snapshot.username:
        match, matched_val, score = check_username_similarity(
            snapshot.username, usernames, NAME_SIMILARITY_THRESHOLD
        )
        if match:
            target = _find_by_username(whitelist, matched_val)
            return DetectionResult(
                flagged=True, match_type="username", matched_val=matched_val,
                score=score, **_target_fields(target)
            )

    # 2 — Homoglyph username
    if snapshot.username and check_homoglyph_danger(snapshot.username):
        return DetectionResult(
            flagged=True, match_type="homoglyph_username",
            matched_val=snapshot.username, score=100.0
        )

    # 3 — Homoglyph name
    if check_homoglyph_danger(full_name):
        return DetectionResult(
            flagged=True, match_type="homoglyph_name",
            matched_val=full_name, score=100.0
        )

    # 4 — Display name similarity
    match, matched_val, score = check_name_similarity(full_name, names, NAME_SIMILARITY_THRESHOLD)
    is_weak = match and (len(full_name.split()) <= 1 or len(matched_val.split()) <= 1)

    if match and not is_weak:
        target = _find_by_name(whitelist, matched_val)
        return DetectionResult(
            flagged=True, match_type="name", matched_val=matched_val,
            score=score, **_target_fields(target)
        )

    # 5 — PFP hash
    if snapshot.pfp_bytes:
        target_hash = compute_pfp_hash_bytes(snapshot.pfp_bytes)
        if target_hash:
            pfp_match, pfp_matched_val, pfp_dist = check_pfp_similarity(
                target_hash, pfp_hashes, PFP_HASH_THRESHOLD
            )
            if pfp_match:
                target = _find_by_pfp(whitelist, pfp_matched_val)
                return DetectionResult(
                    flagged=True, match_type="pfp", matched_val=pfp_matched_val,
                    score=pfp_dist, **_target_fields(target)
                )

    # Weak name match but no PFP match — let them through
    return DetectionResult(flagged=False)


async def ban_and_log(
    result: DetectionResult,
    snapshot: UserSnapshot,
    group_id: int,
    trigger: str,
    ban_func: Callable[[int, int], Awaitable],
    notify_func: Callable[[int, str], Awaitable],
    unban_func: Optional[Callable[[int, int], Awaitable]] = None,
    log_channel_notify: Optional[Callable[[str, Optional[InlineKeyboardMarkup]], Awaitable]] = None,
    invite_link: Optional[str] = None,
):
    """
    Execute a ban/kick/alert based on the group's action_mode, write to DB log,
    and send notifications with inline action buttons on the log channel message.

    ban_func(group_id, user_id)   — wraps bot.ban_chat_member or pyro equivalent.
    unban_func(group_id, user_id) — required for 'kick' mode (ban + immediate unban).
    notify_func(group_id, html)   — sends a message to the group chat.
    log_channel_notify(html, markup) — optional; sends to the log channel with buttons.
    """
    full_name = f"{snapshot.first_name} {snapshot.last_name or ''}".strip()
    target_display = result.target_name or "Unknown"

    # Determine configured action for this group
    group = get_group(group_id)
    action_mode = (group.get("action_mode", "ban") if group else None) or "ban"

    action = "failed"
    if action_mode == "alert":
        action = "alerted"
        logger.info(f"Alert (no ban) for {snapshot.user_id} in {group_id} via {trigger} ({result.match_type})")
    else:
        try:
            await ban_func(group_id, snapshot.user_id)
            if action_mode == "kick" and unban_func:
                await unban_func(group_id, snapshot.user_id)
                action = "kicked"
            else:
                action = "banned"
            logger.info(f"{action.capitalize()} {snapshot.user_id} in {group_id} via {trigger} ({result.match_type})")
        except Exception as e:
            action = f"ban_failed: {e}"
            logger.error(f"Failed to ban {snapshot.user_id} in {group_id}: {e}")

    insert_log(
        group_id=group_id,
        user_id=snapshot.user_id,
        username=snapshot.username,
        full_name=full_name,
        target_user_id=result.target_user_id,
        target_name=result.target_name,
        detection_type=result.match_type,
        similarity_score=result.score,
        action_taken=action,
        details=f"Matched: {result.matched_val}",
        trigger=trigger,
        invite_link=invite_link,
    )

    action_emoji = {"banned": "🚫", "kicked": "👟", "alerted": "⚠️"}.get(action, "❌")
    action_verb  = {"banned": "banned", "kicked": "kicked", "alerted": "flagged (alert only)"}.get(action, action)
    invite_line  = f"\nInvite link: <code>{invite_link}</code>" if invite_link else ""
    group_msg = (
        f"{action_emoji} <b>Impersonator {action_verb}</b>\n"
        f"User: <a href='tg://user?id={snapshot.user_id}'>{full_name}</a> (ID: <code>{snapshot.user_id}</code>)\n"
        f"Reason: Similar <b>{result.match_type}</b> to <b>{target_display}</b>\n"
        f"Match: <code>{result.matched_val}</code> | Score: <code>{result.score}</code>\n"
        f"Trigger: <i>{trigger}</i>{invite_line}"
    )
    try:
        await notify_func(group_id, group_msg)
    except Exception as e:
        logger.error(f"Failed to send group notification: {e}")

    if log_channel_notify:
        log_msg = (
            f"🚨 <b>Impersonation Detected</b>\n\n"
            f"<b>Group ID:</b> <code>{group_id}</code>\n"
            f"<b>User:</b> <a href='tg://user?id={snapshot.user_id}'>{full_name}</a>"
            f" (@{snapshot.username or 'N/A'}) | ID: <code>{snapshot.user_id}</code>\n"
            f"<b>Impersonating:</b> {target_display}"
            f" (ID: <code>{result.target_user_id or 'N/A'}</code>)\n"
            f"<b>Method:</b> {result.match_type}\n"
            f"<b>Match:</b> <code>{result.matched_val}</code>\n"
            f"<b>Score:</b> <code>{result.score}</code>\n"
            f"<b>Trigger:</b> {trigger}\n"
            f"<b>Invite link:</b> {invite_link or 'N/A'}\n"
            f"<b>Action:</b> {action}"
        )
        # Attach action buttons only when the user was actually banned/kicked
        keyboard = None
        if action in ("banned", "kicked"):
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Unban + Whitelist",
                    callback_data=f"unban_wl|{group_id}|{snapshot.user_id}",
                ),
                InlineKeyboardButton(
                    "🗑 Dismiss",
                    callback_data=f"dismiss|{group_id}|{snapshot.user_id}",
                ),
            ]])
        try:
            await log_channel_notify(log_msg, keyboard)
        except Exception as e:
            logger.error(f"Failed to send log channel notification: {e}")


# ── Private helpers ────────────────────────────────────────────────────────────

def _target_fields(row: Optional[dict]) -> dict:
    if not row:
        return {"target_user_id": None, "target_name": None}
    name = f"{row['first_name']} {row['last_name'] or ''}".strip()
    return {"target_user_id": row["user_id"], "target_name": name}


def _find_by_username(whitelist: list[dict], username: str) -> Optional[dict]:
    u = username.lower()
    return next((w for w in whitelist if w["username"] and w["username"].lower() == u), None)


def _find_by_name(whitelist: list[dict], name: str) -> Optional[dict]:
    return next(
        (w for w in whitelist if f"{w['first_name']} {w['last_name'] or ''}".strip() == name),
        None
    )


def _find_by_pfp(whitelist: list[dict], pfp_hash: str) -> Optional[dict]:
    return next((w for w in whitelist if w["pfp_hash"] == pfp_hash), None)
