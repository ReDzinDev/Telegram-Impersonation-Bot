
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
    )
    return _client


def get_client() -> Optional[Client]:
    return _client
