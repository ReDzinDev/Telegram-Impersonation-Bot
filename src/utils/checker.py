
"""
Shared impersonation check and ban logic.
Called from: join handler, message handler, sweep, and Pyrogram profile-change watcher.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable

from telegram import InlineKeyboardMarkup, InlineKeyboardButton

from src.db import (
    get_whitelist, is_whitelisted, insert_log, get_group, get_reserved_keywords,
    is_false_positive, mark_false_positive,
)
from src.utils.detector import (
    check_username_similarity, check_name_similarity,
    check_homoglyph_danger, check_reserved_keywords,
)
from src.utils.image import compute_pfp_hash_bytes, check_pfp_similarity
from src.config import NAME_SIMILARITY_THRESHOLD, PFP_HASH_THRESHOLD

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
    bio: Optional[str] = None           # Telegram bio / about text (Pyrogram only)


@dataclass
class DetectionResult:
    flagged: bool
    needs_pfp: bool = False              # True = weak name match; caller should fetch PFP and re-run
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

    # Skip users within their false-positive grace window
    if is_false_positive(group_id, snapshot.user_id):
        return DetectionResult(flagged=False)

    # Per-group similarity threshold (falls back to global config)
    group_cfg = get_group(group_id)
    threshold = (group_cfg.get("similarity_threshold") if group_cfg else None) or NAME_SIMILARITY_THRESHOLD

    whitelist = get_whitelist(group_id)
    full_name = f"{snapshot.first_name} {snapshot.last_name or ''}".strip()

    # 0 — Reserved keyword / regex check (fastest — pure string ops, no fuzzy scoring)
    keywords = get_reserved_keywords(group_id)
    if keywords:
        matched_kw = check_reserved_keywords(full_name, snapshot.username, snapshot.bio, keywords)
        if matched_kw:
            return DetectionResult(
                flagged=True, match_type="keyword",
                matched_val=matched_kw, score=100.0,
            )

    if not whitelist:
        return DetectionResult(flagged=False)

    # Exclude the user's own whitelist entry so they can never match themselves
    others = [w for w in whitelist if w["user_id"] != snapshot.user_id]
    if not others:
        return DetectionResult(flagged=False)

    usernames  = [w["username"] for w in others if w["username"]]
    names      = [f"{w['first_name']} {w['last_name'] or ''}".strip() for w in others]
    pfp_hashes = [w["pfp_hash"] for w in others if w["pfp_hash"]]

    # 1 — Username similarity (username vs whitelist usernames only)
    if snapshot.username and usernames:
        match, matched_val, score = check_username_similarity(
            snapshot.username, usernames, threshold
        )
        if match:
            target = _find_by_username(others, matched_val)
            return DetectionResult(
                flagged=True, match_type="username", matched_val=matched_val,
                score=score, **_target_fields(target)
            )

    # 2 — Homoglyph username: only flag if it also fuzzy-matches a whitelisted username
    if snapshot.username and usernames and check_homoglyph_danger(snapshot.username):
        match, matched_val, score = check_username_similarity(
            snapshot.username, usernames, threshold
        )
        if match:
            target = _find_by_username(others, matched_val)
            return DetectionResult(
                flagged=True, match_type="homoglyph_username",
                matched_val=matched_val, score=score, **_target_fields(target)
            )

    # 3 — Homoglyph name: only flag if it also fuzzy-matches a whitelisted display name
    if check_homoglyph_danger(full_name):
        match, matched_val, score = check_name_similarity(full_name, names, threshold)
        if match and not (len(full_name.split()) <= 1 or len(matched_val.split()) <= 1):
            target = _find_by_name(others, matched_val)
            return DetectionResult(
                flagged=True, match_type="homoglyph_name",
                matched_val=matched_val, score=score, **_target_fields(target)
            )

    # 4 — Display name similarity (name vs whitelist names only)
    match, matched_val, score = check_name_similarity(full_name, names, threshold)
    is_weak = match and (len(full_name.split()) <= 1 or len(matched_val.split()) <= 1)

    if match and not is_weak:
        target = _find_by_name(others, matched_val)
        return DetectionResult(
            flagged=True, match_type="name", matched_val=matched_val,
            score=score, **_target_fields(target)
        )

    # 5 — PFP hash (tiebreaker for weak name matches only)
    # A standalone photo match without any name/username similarity is too noisy.
    if is_weak and pfp_hashes:
        if not snapshot.pfp_bytes:
            # Signal the caller to fetch the PFP and re-run (lazy loading for sweep)
            return DetectionResult(flagged=False, needs_pfp=True)
        target_hash = compute_pfp_hash_bytes(snapshot.pfp_bytes)
        if target_hash:
            pfp_match, pfp_matched_val, pfp_dist = check_pfp_similarity(
                target_hash, pfp_hashes, PFP_HASH_THRESHOLD
            )
            if pfp_match:
                target = _find_by_pfp(others, pfp_matched_val)
                return DetectionResult(
                    flagged=True, match_type="pfp", matched_val=pfp_matched_val,
                    score=pfp_dist, **_target_fields(target)
                )

    # 6 — Group identity: catch users impersonating the group itself.
    #     Checks user name similarity to the group title, and (for weak matches)
    #     user PFP similarity to the group's stored logo hash.
    if group_cfg:
        group_title    = group_cfg.get("title") or ""
        group_pfp_hash = group_cfg.get("pfp_hash")

        if group_title:
            g_match, g_matched, g_score = check_name_similarity(full_name, [group_title], threshold)
            g_is_weak = g_match and (
                len(full_name.split()) <= 1 or len(group_title.split()) <= 1
            )

            if g_match and not g_is_weak:
                # Strong name match to the group itself (multi-word, above threshold)
                return DetectionResult(
                    flagged=True, match_type="group_name",
                    matched_val=group_title, score=g_score,
                    target_name=f"[Group] {group_title}",
                )

            # Weak group-name match: use the group logo as tiebreaker
            if g_is_weak and group_pfp_hash:
                if not snapshot.pfp_bytes:
                    return DetectionResult(flagged=False, needs_pfp=True)
                g_user_hash = compute_pfp_hash_bytes(snapshot.pfp_bytes)
                if g_user_hash:
                    g_pfp_match, _, g_pfp_dist = check_pfp_similarity(
                        g_user_hash, [group_pfp_hash], PFP_HASH_THRESHOLD
                    )
                    if g_pfp_match:
                        return DetectionResult(
                            flagged=True, match_type="group_pfp",
                            matched_val=group_title, score=g_pfp_dist,
                            target_name=f"[Group] {group_title}",
                        )

    return DetectionResult(flagged=False)


async def ban_and_log(
    result: DetectionResult,
    snapshot: UserSnapshot,
    group_id: int,
    trigger: str,
    ban_func: Callable[[int, int], Awaitable],
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

    if log_channel_notify:
        log_msg = (
            f"🚨 <b>Impersonation Detected</b>\n\n"
            f"<b>Group ID:</b> <code>{group_id}</code>\n"
            f"<b>User:</b> <a href='tg://user?id={snapshot.user_id}'>{html.escape(full_name)}</a>"
            f" (@{html.escape(snapshot.username or 'N/A')}) | ID: <code>{snapshot.user_id}</code>\n"
            f"<b>Impersonating:</b> {html.escape(target_display)}"
            f" (ID: <code>{result.target_user_id or 'N/A'}</code>)\n"
            f"<b>Method:</b> {result.match_type}\n"
            f"<b>Match:</b> <code>{html.escape(str(result.matched_val))}</code>\n"
            f"<b>Score:</b> <code>{result.score}</code>\n"
            f"<b>Trigger:</b> {trigger}\n"
            f"<b>Invite link:</b> {invite_link or 'N/A'}\n"
            f"<b>Action:</b> {action}"
        )
        # Attach action buttons only when the user was actually banned/kicked
        keyboard = None
        if action in ("banned", "kicked"):
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Unban + Whitelist",
                        callback_data=f"unban_wl|{group_id}|{snapshot.user_id}",
                    ),
                    InlineKeyboardButton(
                        "🔓 Unban only",
                        callback_data=f"unban_fp|{group_id}|{snapshot.user_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🗑 Dismiss",
                        callback_data=f"dismiss|{group_id}|{snapshot.user_id}",
                    ),
                ],
            ])
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
