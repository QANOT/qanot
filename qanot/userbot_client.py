"""Shared Pyrogram (MTProto) client used by voicecall and the userbot plugin.

Both features sign in as the human operator's account using the same
``voicecall_api_id`` / ``voicecall_api_hash`` / ``voicecall_session``
credentials. Running two Pyrogram ``Client`` instances against the same
session causes auth-key desync errors and randomly drops connections —
so they share a single process-wide instance managed here.

Public surface:

    client = await get_userbot_client(config)   # idempotent; returns a
                                                # started Client or None
                                                # if no session configured
    await shutdown_userbot_client()             # call at bot shutdown

The client is created with ``no_updates=True``. This is safe for the
current consumers (voicecall, plus plugin tools that only *pull* data
via explicit API methods). If a future feature needs to receive raw
update events from pyrogram itself, flip ``no_updates`` and ensure the
caller can actually handle the event volume of a real Telegram account.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from qanot.config import Config

logger = logging.getLogger(__name__)

_client: Any = None  # pyrogram.Client when started
_client_lock: asyncio.Lock = asyncio.Lock()


async def get_userbot_client(config: "Config") -> Any:
    """Return a started Pyrogram Client, creating it on first call.

    Returns ``None`` if the session / api credentials aren't configured
    — callers must handle that gracefully and surface a clear error to
    the user instead of crashing.
    """
    global _client

    if not config.voicecall_api_id or not config.voicecall_api_hash:
        return None
    if not config.voicecall_session:
        return None

    async with _client_lock:
        if _client is not None:
            return _client

        try:
            from pyrogram import Client
        except ImportError as e:
            raise RuntimeError(
                "pyrofork not installed — userbot features require "
                "pip install pyrofork"
            ) from e

        client = Client(
            name="qanot_userbot",
            api_id=config.voicecall_api_id,
            api_hash=config.voicecall_api_hash,
            session_string=config.voicecall_session,
            no_updates=True,
            in_memory=True,
        )
        await client.start()
        me = await client.get_me()
        logger.info(
            "Userbot client started for @%s (id=%d)",
            me.username or me.first_name, me.id,
        )
        _client = client
        return _client


async def shutdown_userbot_client() -> None:
    """Stop the shared client. Safe to call multiple times."""
    global _client
    async with _client_lock:
        if _client is None:
            return
        try:
            await _client.stop()
        except Exception as e:
            logger.debug("Userbot client stop error: %s", e)
        _client = None


def is_client_started() -> bool:
    """True if the client is currently running."""
    return _client is not None
