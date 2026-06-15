"""
Shared Pyrogram (MTProto) fetch helpers.

Profile-photo and bio fetching were previously duplicated across sweep.py,
events.py, and commands.py — three slightly-different copies of the same
stream-media / GetFullUser logic. Centralizing them here removes the
duplication (and the member_join -> events import cycle that existed only
to reach _fetch_bio).
"""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional

from pyrogram import Client, raw
from pyrogram.errors import FloodWait

from src.utils.image import compute_pfp_hash_bytes

logger = logging.getLogger(__name__)


async def fetch_pfp_bytes(pyro: Client, user_id: int) -> Optional[bytes]:
    """Download a user's current profile photo as raw bytes, or None."""
    try:
        photos = pyro.get_chat_photos(user_id, limit=1)
        photo = await photos.__anext__()
        buf = BytesIO()
        async for chunk in pyro.stream_media(photo):
            buf.write(chunk)
        return buf.getvalue() or None
    except StopAsyncIteration:
        return None
    except FloodWait as e:
        # DC-level rate limit on media downloads — skip this PFP rather than
        # blocking a whole sweep for potentially 20+ minutes.
        logger.warning(f"PFP flood wait {e.value}s for user {user_id} — skipping photo check.")
        return None
    except Exception:
        return None


async def fetch_pfp_hash(pyro: Client, user_id: int) -> Optional[str]:
    """Download + perceptual-hash a user's profile photo, or None."""
    data = await fetch_pfp_bytes(pyro, user_id)
    return compute_pfp_hash_bytes(data) if data else None


async def fetch_bio(pyro: Client, user_id: int) -> Optional[str]:
    """Fetch a user's bio / about text via MTProto GetFullUser, or None."""
    try:
        peer = await pyro.resolve_peer(user_id)
        full = await pyro.invoke(raw.functions.users.GetFullUser(id=peer))
        return full.full_user.about or None
    except Exception:
        return None
