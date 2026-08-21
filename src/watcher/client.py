
"""
Pyrogram user client (MTProto watcher).

Provides full-member enumeration and real-time profile-change events —
capabilities that the Bot API does not expose.

First-time setup (run once locally):
    python -c "
    from pyrogram import Client
    import asyncio, os
    from dotenv import load_dotenv
    load_dotenv()
    async def main():
        async with Client('session', api_id=os.getenv('PYROGRAM_API_ID'),
                          api_hash=os.getenv('PYROGRAM_API_HASH')) as app:
            print(await app.export_session_string())
    asyncio.run(main())
    "
Then set PYROGRAM_SESSION=<output> in your .env / Railway environment.
"""
from __future__ import annotations

import logging
from typing import Optional

from pyrogram import Client

logger = logging.getLogger(__name__)

_client: Optional[Client] = None


def build_client(api_id: str, api_hash: str, session_string: str) -> Client:
    global _client
    _client = Client(
        name="watcher",
        api_id=int(api_id),
        api_hash=api_hash,
        session_string=session_string,
        # No phone/password needed when using a session string
        #
        # Surface every FloodWait to the caller. Session.SLEEP_THRESHOLD
        # defaults to 10, and Session.invoke swallows any FloodWait at or under
        # it — sleeping internally and retrying, raising nothing. That made
        # every mild, early pushback invisible to the adaptive pacer in
        # src.watcher.fetch: on_flood never fired, the interval never ratcheted,
        # and the pacer reported a healthy system while Telegram was already
        # throttling us. We would rather back off deliberately than have the
        # library hide the signal and let us keep pushing.
        #
        # This must be set on the Client, not per call: invoke() treats a falsy
        # per-call sleep_threshold as "unset" and falls back to the default.
        sleep_threshold=0,
    )
    return _client


def get_client() -> Optional[Client]:
    return _client
