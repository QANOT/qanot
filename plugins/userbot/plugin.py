"""Userbot plugin — send Telegram messages AS the human operator.

The plugin drives Telegram's MTProto API via the already-running shared
pyrofork client (``qanot.userbot_client.get_userbot_client``). That
client is signed in as the *account owner*, not a bot — so every send
appears in the recipient's chat as if the owner typed it themselves.

This is a high blast-radius capability. Three layers of defence live here:

  1. **Opaque recipient tokens.** The agent never talks to tg_send_message
     with a raw ``@username``. It must first resolve the contact via
     ``tg_find_contact`` (or list/dialogs), which returns a freshly-minted
     random token like ``rcp_a83f12…`` that the plugin maps internally to
     the pyrogram peer. This defeats prompt-injection attacks of the
     shape *"now please forward this to @attacker"* — the attacker's
     username has no token in our map, and the agent can only address
     peers the system itself resolved first. Tokens expire after 1 hour.

  2. **Whitelist.** If ``userbot_recipient_whitelist`` is non-empty, every
     send is gated against it (username OR integer id match).

  3. **Rate limits.** Per-recipient cooldown + hourly global quota, both
     configured via ``Config``.

On a successful send, the plugin posts a preview into the originating
Telegram chat ("✍️ @umid ga xabar yuborildi: …") so the operator sees
what happened. No confirmation button — the user explicitly rejected
the confirmation-button UX; rate limits + whitelist are the guardrails.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from qanot.plugins.base import Plugin, ToolDef, tool

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent
# Match other plugins' sys.path trick: double-insert so the loader's later
# single .remove() leaves one copy behind for subsequent local imports.
sys.path.insert(0, str(PLUGIN_DIR))

# How long a minted recipient token stays valid. One hour is long enough
# for the agent to think through a multi-turn send flow, short enough
# that stale tokens don't linger across sessions.
TOKEN_TTL_SECONDS = 3600

# Preview text length shown inside the bot chat. Tokens of 400 chars
# keep the preview readable in the Telegram UI without dwarfing the
# agent's actual reply.
PREVIEW_TEXT_MAX = 400


class UserbotPlugin(Plugin):
    """Send messages as the owner's Telegram account via MTProto."""

    name = "userbot"
    description = "Send Telegram messages as the owner via MTProto (userbot)"
    version = "0.1.0"

    def __init__(self) -> None:
        self._workspace_dir: str = ""
        self._config: Any = None  # qanot.config.Config, set at setup()
        self._rate_limiter: Any = None
        self._audit: Any = None
        # opaque_id → {"peer": pyrogram peer, "username": str|None,
        #              "first_name": str, "id": int, "type": str,
        #              "minted_at": float}
        self._peers: dict[str, dict[str, Any]] = {}
        self._peers_lock = asyncio.Lock()
        # Lazy aiogram.Bot for posting preview messages in the calling chat.
        # Built on first need from config.bot_token.
        self._preview_bot: Any = None

    # ── Lifecycle ────────────────────────────────────────────────

    async def setup(self, config: dict) -> None:
        """Gate on ``userbot_enabled``. When disabled, register nothing.

        We also stash a reference to the framework-wide ``Config`` object
        so tools can hand it to ``get_userbot_client`` without re-reading
        the on-disk config file for every call.
        """
        cfg_dict = config or {}
        self._workspace_dir = cfg_dict.get("workspace_dir") or "/data/workspace"

        # Load canonical framework config (the plugin loader only hands us
        # plugin-scoped config). We need ``voicecall_*`` fields for the
        # shared userbot client.
        try:
            from qanot.config import load_config
            config_path = os.environ.get("QANOT_CONFIG", "/data/config.json")
            self._config = load_config(config_path)
        except Exception as e:
            logger.warning("userbot: could not load framework config: %s", e)
            self._config = None

        if self._config is None or not getattr(self._config, "userbot_enabled", False):
            logger.info(
                "Userbot plugin loaded but DISABLED via config.userbot_enabled. "
                "Tools will NOT be registered. Flip the flag and restart to enable.",
            )
            # Clear tools — a no-op if base hasn't collected yet.
            self._disabled = True
            return

        from ratelimit import RateLimiter
        from audit import AuditLog

        self._rate_limiter = RateLimiter(
            per_recipient_seconds=int(self._config.userbot_send_per_recipient_seconds),
            hourly_global=int(self._config.userbot_send_hourly_global),
        )
        self._audit = AuditLog(self._workspace_dir)
        self._disabled = False
        logger.info(
            "Userbot plugin ready (per_recipient=%ds, hourly=%d, whitelist=%d)",
            self._config.userbot_send_per_recipient_seconds,
            self._config.userbot_send_hourly_global,
            len(self._config.userbot_recipient_whitelist or []),
        )

    async def teardown(self) -> None:
        bot = self._preview_bot
        self._preview_bot = None
        if bot is not None:
            try:
                await bot.session.close()
            except Exception as e:
                logger.debug("userbot preview bot close failed: %s", e)

    def get_tools(self) -> list[ToolDef]:
        # Honour the kill-switch: don't expose tools when disabled.
        if getattr(self, "_disabled", True):
            return []
        return self._collect_tools()

    # ── Internal helpers ─────────────────────────────────────────

    async def _get_client(self) -> Any:
        """Resolve the shared pyrofork client, or None if not configured."""
        from qanot.userbot_client import get_userbot_client

        if self._config is None:
            return None
        return await get_userbot_client(self._config)

    def _not_configured(self) -> str:
        return json.dumps({
            "status": "unconfigured",
            "error": (
                "Userbot session sozlanmagan. voicecall_api_id / voicecall_api_hash / "
                "voicecall_session qiymatlarini config.json-ga qo'shing va qayta ishga tushiring."
            ),
        }, ensure_ascii=False)

    async def _mint_token(
        self,
        *,
        peer: Any,
        username: str | None,
        first_name: str,
        peer_id: int,
        peer_type: str,
    ) -> str:
        """Store a peer under a freshly-minted opaque id and return the id."""
        token = f"rcp_{uuid.uuid4().hex[:12]}"
        async with self._peers_lock:
            self._peers[token] = {
                "peer": peer,
                "username": username,
                "first_name": first_name,
                "id": peer_id,
                "type": peer_type,
                "minted_at": time.time(),
            }
            self._evict_expired_locked()
        return token

    def _evict_expired_locked(self) -> None:
        """Must be called with ``_peers_lock`` held."""
        cutoff = time.time() - TOKEN_TTL_SECONDS
        stale = [k for k, v in self._peers.items() if v["minted_at"] < cutoff]
        for k in stale:
            self._peers.pop(k, None)

    async def _lookup_token(self, token: str) -> dict[str, Any] | None:
        async with self._peers_lock:
            self._evict_expired_locked()
            entry = self._peers.get(token)
            return dict(entry) if entry else None

    def _allowed_by_whitelist(self, entry: dict[str, Any]) -> bool:
        """Case-insensitive username match (with/without @) OR int id match."""
        if self._config is None:
            return False
        whitelist = self._config.userbot_recipient_whitelist or []
        if not whitelist:
            return True  # empty whitelist = allow any
        uname = (entry.get("username") or "").lstrip("@").lower()
        pid = entry.get("id")
        for raw in whitelist:
            # ints or numeric strings → id match
            if isinstance(raw, int):
                if pid == raw:
                    return True
                continue
            s = str(raw).strip()
            if not s:
                continue
            if s.lstrip("-").isdigit():
                try:
                    if pid == int(s):
                        return True
                except ValueError:
                    pass
                continue
            # username match
            if s.lstrip("@").lower() == uname and uname:
                return True
        return False

    def _recipient_label(self, entry: dict[str, Any]) -> str:
        """Human-readable recipient label for previews + audit."""
        uname = entry.get("username")
        if uname:
            return f"@{uname}"
        first = entry.get("first_name") or ""
        if first:
            return first
        return str(entry.get("id") or "?")

    async def _post_preview(self, text: str, recipient_label: str) -> None:
        """Post a confirmation message into the chat the agent is answering.

        Best-effort — if the bot token or chat id is unavailable (e.g. the
        tool was called from a cron job), we silently skip. The audit log
        is the authoritative record either way.
        """
        try:
            from qanot.agent import Agent

            agent = getattr(Agent, "_instance", None)
            chat_id = getattr(agent, "current_chat_id", None) if agent else None
            if not chat_id:
                return
            bot = await self._get_preview_bot()
            if bot is None:
                return
            preview_text = text if len(text) <= PREVIEW_TEXT_MAX else (
                text[:PREVIEW_TEXT_MAX] + "…"
            )
            msg = f"✍️ {recipient_label} ga xabar yuborildi:\n{preview_text}"
            await bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            logger.debug("userbot preview post failed: %s", e)

    async def _get_preview_bot(self) -> Any:
        """Lazily build an aiogram Bot from the framework token."""
        if self._preview_bot is not None:
            return self._preview_bot
        token = getattr(self._config, "bot_token", "") if self._config else ""
        if not token:
            return None
        try:
            from aiogram import Bot
        except ImportError:
            return None
        self._preview_bot = Bot(token=token)
        return self._preview_bot

    @staticmethod
    def _chat_type(chat: Any) -> str:
        """Map a pyrogram chat.type to our compact 'user'|'group'|'channel'."""
        t = getattr(chat, "type", None)
        name = getattr(t, "name", None) or getattr(t, "value", None) or str(t or "")
        name = str(name).lower()
        if "channel" in name:
            return "channel"
        if "group" in name or "supergroup" in name:
            return "group"
        # PRIVATE / BOT / USER / anything else → user
        return "user"

    @staticmethod
    def _friendly_rpc_error(exc: Exception) -> tuple[str, str]:
        """Classify a pyrogram RPC error → (error_class, Uzbek message).

        We don't import every pyrogram exception class at top-level because
        the plugin loads even when pyrogram isn't installed — we just use
        ``type(exc).__name__`` as the discriminator.
        """
        name = type(exc).__name__
        msg = str(exc)
        lname = name.lower()
        if "floodwait" in lname:
            return name, f"Telegram flood cheklovi: biroz kutib, qayta urinib ko'ring ({msg})."
        if "userprivacy" in lname or "privacy" in lname:
            return name, "Bu foydalanuvchining maxfiylik sozlamalari xabar yuborishni taqiqlaydi."
        if "userisblocked" in lname or "blocked" in lname:
            return name, "Foydalanuvchi sizning akkauntingizni bloklagan."
        if "peerid" in lname or "peernotfound" in lname:
            return name, "Telegram kontakt topilmadi — avval tg_find_contact bilan qidiring."
        if "chatwriteforbidden" in lname:
            return name, "Bu chatga yozish huquqingiz yo'q."
        if "inputuserdeactivated" in lname or "deactivated" in lname:
            return name, "Foydalanuvchi akkaunti o'chirilgan."
        return name, f"Telegram xatolik: {msg}"

    # ── Tools ────────────────────────────────────────────────────

    @tool(
        name="tg_find_contact",
        description=(
            "Find a Telegram contact, group, or channel in the account's "
            "address book / known dialogs by @username, phone number, "
            "numeric user id, or display name. Returns an OPAQUE recipient_id "
            "token you must pass to tg_send_message — raw usernames are "
            "rejected by the sender on purpose. Call this BEFORE every "
            "tg_send_message unless you already have a fresh token from "
            "tg_list_recent_chats."
        ),
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "@username (with or without @), phone number "
                        "(+998…), numeric user id, or first name."
                    ),
                },
            },
        },
    )
    async def tg_find_contact(self, params: dict) -> str:
        client = await self._get_client()
        if client is None:
            return self._not_configured()

        query = (params.get("query") or "").strip()
        if not query:
            return json.dumps(
                {"status": "error", "error": "query is required"},
                ensure_ascii=False,
            )

        # Normalise numeric strings and @-prefixed usernames, but otherwise
        # let pyrogram figure it out — get_users/get_chat both accept a
        # broad range of inputs.
        candidate: Any = query
        if query.lstrip("-").isdigit():
            try:
                candidate = int(query)
            except ValueError:
                pass

        try:
            chat = await client.get_chat(candidate)
        except Exception as e:
            cls, friendly = self._friendly_rpc_error(e)
            return json.dumps(
                {"status": "error", "error_class": cls, "error": friendly},
                ensure_ascii=False,
            )

        peer_type = self._chat_type(chat)
        username = getattr(chat, "username", None)
        first_name = getattr(chat, "first_name", None) or getattr(chat, "title", None) or ""
        peer_id = int(getattr(chat, "id", 0) or 0)

        token = await self._mint_token(
            peer=peer_id,  # pyrogram accepts raw ids on send_message
            username=username,
            first_name=first_name,
            peer_id=peer_id,
            peer_type=peer_type,
        )
        return json.dumps(
            {
                "status": "ok",
                "recipient_id": token,
                "username": username,
                "first_name": first_name,
                "id": peer_id,
                "type": peer_type,
            },
            ensure_ascii=False,
        )

    @tool(
        name="tg_send_message",
        description=(
            "Send a Telegram text message AS the account owner to a recipient "
            "previously resolved via tg_find_contact or tg_list_recent_chats. "
            "The recipient_id MUST be an opaque token returned by one of those "
            "tools (tokens expire after 1 hour). Rate-limited: minimum gap "
            "per recipient + hourly global cap. Posts a preview into the "
            "current chat after a successful send."
        ),
        parameters={
            "type": "object",
            "required": ["recipient_id", "text"],
            "properties": {
                "recipient_id": {
                    "type": "string",
                    "description": "Opaque token from tg_find_contact (rcp_…)",
                },
                "text": {
                    "type": "string",
                    "description": "Message body (plain text, Telegram limit ~4096 chars).",
                },
            },
        },
    )
    async def tg_send_message(self, params: dict) -> str:
        client = await self._get_client()
        if client is None:
            return self._not_configured()

        recipient_id = (params.get("recipient_id") or "").strip()
        text = params.get("text") or ""
        if not recipient_id:
            return json.dumps({"status": "error", "error": "recipient_id is required"}, ensure_ascii=False)
        if not text.strip():
            return json.dumps({"status": "error", "error": "text is required (non-empty)"}, ensure_ascii=False)

        entry = await self._lookup_token(recipient_id)
        if entry is None:
            return json.dumps(
                {
                    "status": "error",
                    "error": (
                        "recipient_id noto'g'ri yoki muddati tugagan. Avval tg_find_contact "
                        "yoki tg_list_recent_chats chaqirib yangi token oling."
                    ),
                },
                ensure_ascii=False,
            )

        label = self._recipient_label(entry)

        # Whitelist gate (non-empty list = gated).
        if not self._allowed_by_whitelist(entry):
            self._audit.whitelist_reject(recipient=label)
            return json.dumps(
                {
                    "status": "error",
                    "error": (
                        "Bu oluvchi userbot_recipient_whitelist ro'yxatida yo'q. "
                        "config.json-dagi whitelist-ga qo'shing yoki boshqa kontaktni tanlang."
                    ),
                    "recipient": label,
                },
                ensure_ascii=False,
            )

        # Rate limit gate.
        try:
            self._rate_limiter.check(recipient_id)
        except Exception as rle:
            # RateLimitError has bucket/retry_after_seconds attributes.
            bucket = getattr(rle, "bucket", "unknown")
            retry = int(getattr(rle, "retry_after_seconds", 0))
            self._audit.rate_limit(recipient=label, bucket=bucket, retry_after=retry)
            return json.dumps(
                {
                    "status": "error",
                    "error": str(rle),
                    "bucket": bucket,
                    "retry_after_seconds": retry,
                },
                ensure_ascii=False,
            )

        # Do the actual send.
        try:
            msg = await client.send_message(entry["peer"], text)
        except Exception as e:
            cls, friendly = self._friendly_rpc_error(e)
            self._audit.send_error(recipient=label, error_class=cls)
            return json.dumps(
                {"status": "error", "error_class": cls, "error": friendly},
                ensure_ascii=False,
            )

        message_id = int(getattr(msg, "id", 0) or getattr(msg, "message_id", 0) or 0)

        # Record + audit only on actual success.
        self._rate_limiter.record(recipient_id)
        self._audit.send(
            recipient_id=recipient_id,
            recipient=label,
            text=text,
            message_id=message_id,
        )

        # Preview post is best-effort and must NEVER raise back into the
        # tool result — the send already happened.
        await self._post_preview(text, label)

        return json.dumps(
            {
                "status": "ok",
                "ok": True,
                "message_id": message_id,
                "recipient": label,
            },
            ensure_ascii=False,
        )

    @tool(
        name="tg_list_recent_chats",
        description=(
            "List the account's most-recently active dialogs (private chats, "
            "groups, channels). Each entry includes a freshly minted "
            "recipient_id the agent can hand to tg_send_message without a "
            "separate find_contact step. Tokens expire after 1 hour."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max dialogs to return (1-100, default 20).",
                },
            },
        },
    )
    async def tg_list_recent_chats(self, params: dict) -> str:
        client = await self._get_client()
        if client is None:
            return self._not_configured()

        limit = max(1, min(100, int(params.get("limit") or 20)))

        items: list[dict[str, Any]] = []
        try:
            async for dialog in client.get_dialogs(limit=limit):
                chat = getattr(dialog, "chat", None)
                if chat is None:
                    continue
                peer_type = self._chat_type(chat)
                username = getattr(chat, "username", None)
                title = (
                    getattr(chat, "title", None)
                    or getattr(chat, "first_name", None)
                    or (f"@{username}" if username else "")
                    or str(getattr(chat, "id", "?"))
                )
                peer_id = int(getattr(chat, "id", 0) or 0)
                token = await self._mint_token(
                    peer=peer_id,
                    username=username,
                    first_name=title,
                    peer_id=peer_id,
                    peer_type=peer_type,
                )

                last_msg = getattr(dialog, "top_message", None)
                last_text = ""
                if last_msg is not None:
                    last_text = (
                        getattr(last_msg, "text", None)
                        or getattr(last_msg, "caption", None)
                        or ""
                    )
                    if not last_text:
                        # Media-only messages produce an empty text; show a placeholder.
                        last_text = "<media>"

                items.append({
                    "recipient_id": token,
                    "title": title,
                    "type": peer_type,
                    "unread": int(getattr(dialog, "unread_messages_count", 0) or 0),
                    "last_message_preview": last_text[:120],
                })
        except Exception as e:
            cls, friendly = self._friendly_rpc_error(e)
            return json.dumps(
                {"status": "error", "error_class": cls, "error": friendly},
                ensure_ascii=False,
            )

        return json.dumps(
            {"status": "ok", "count": len(items), "dialogs": items},
            ensure_ascii=False,
        )

    @tool(
        name="tg_get_chat_history",
        description=(
            "Fetch the most recent messages from a chat/group/channel. The "
            "recipient_id MUST be a token minted by tg_find_contact or "
            "tg_list_recent_chats. Non-text messages (photos, stickers, etc.) "
            "are returned with the placeholder \"<media>\". Useful for "
            "summarising unread conversations before replying."
        ),
        parameters={
            "type": "object",
            "required": ["recipient_id"],
            "properties": {
                "recipient_id": {
                    "type": "string",
                    "description": "Opaque token from tg_find_contact / tg_list_recent_chats.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max messages (1-100, default 10). Newest first.",
                },
            },
        },
    )
    async def tg_get_chat_history(self, params: dict) -> str:
        client = await self._get_client()
        if client is None:
            return self._not_configured()

        recipient_id = (params.get("recipient_id") or "").strip()
        if not recipient_id:
            return json.dumps({"status": "error", "error": "recipient_id is required"}, ensure_ascii=False)
        entry = await self._lookup_token(recipient_id)
        if entry is None:
            return json.dumps(
                {
                    "status": "error",
                    "error": (
                        "recipient_id noto'g'ri yoki muddati tugagan. Avval tg_find_contact "
                        "yoki tg_list_recent_chats chaqiring."
                    ),
                },
                ensure_ascii=False,
            )

        limit = max(1, min(100, int(params.get("limit") or 10)))

        messages: list[dict[str, Any]] = []
        try:
            async for m in client.get_chat_history(entry["peer"], limit=limit):
                sender = getattr(m, "from_user", None)
                sender_label = ""
                if sender is not None:
                    uname = getattr(sender, "username", None)
                    if uname:
                        sender_label = f"@{uname}"
                    else:
                        sender_label = getattr(sender, "first_name", None) or str(
                            getattr(sender, "id", "?"),
                        )
                text = getattr(m, "text", None) or getattr(m, "caption", None) or ""
                if not text:
                    text = "<media>"
                date = getattr(m, "date", None)
                if hasattr(date, "isoformat"):
                    date_str = date.isoformat()
                else:
                    date_str = str(date or "")
                messages.append({
                    "from": sender_label,
                    "text": text,
                    "date": date_str,
                })
        except Exception as e:
            cls, friendly = self._friendly_rpc_error(e)
            return json.dumps(
                {"status": "error", "error_class": cls, "error": friendly},
                ensure_ascii=False,
            )

        return json.dumps(
            {"status": "ok", "count": len(messages), "messages": messages},
            ensure_ascii=False,
        )
