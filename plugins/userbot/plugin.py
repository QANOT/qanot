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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
        # Display timezone for message timestamps. Set during setup() from
        # ``Config.timezone``. The agent reads ISO strings and quotes times
        # back to the operator — those must be in the operator's local zone,
        # not pyrogram's UTC default, or a 5-hour Tashkent shift looks like
        # "messages from the future" / "from last night".
        self._tz: Any = timezone.utc

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

        tz_name = (getattr(self._config, "timezone", None) or "UTC").strip() or "UTC"
        try:
            self._tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            logger.warning("userbot: unknown timezone %r, falling back to UTC", tz_name)
            self._tz = timezone.utc

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

    def _format_date(self, date: Any) -> str:
        """Render a pyrogram Message.date as an ISO string in the operator's
        local timezone.

        Pyrogram emits UTC-aware datetimes by default. The agent reads the
        ISO string and quotes the wall-clock back to the operator, so we
        must shift to ``Config.timezone`` here — otherwise an Asia/Tashkent
        operator sees "03:13" for a message that arrived at 08:13 local."""
        if date is None:
            return ""
        if not hasattr(date, "isoformat"):
            return str(date)
        # Defensive: a naive datetime is unspecified in the docs but has
        # showed up in older pyrogram builds. Treat it as UTC.
        if getattr(date, "tzinfo", None) is None:
            try:
                date = date.replace(tzinfo=timezone.utc)
            except Exception:
                return str(date)
        try:
            return date.astimezone(self._tz).isoformat()
        except Exception:
            # Whatever weird datetime subclass this is, fall back to the
            # original isoformat rather than dropping the timestamp.
            return date.isoformat()

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
            "current chat after a successful send. Use dry_run=true to "
            "prepare a draft for the operator to review without sending."
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
                "reply_to_message_id": {
                    "type": "integer",
                    "description": (
                        "Optional. If set, send as a reply to this message "
                        "id within the same chat. Use only when the operator "
                        "explicitly asks to thread the reply."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "If true, do NOT send. Return what would be sent so "
                        "the operator can review. Whitelist is still enforced; "
                        "rate limit is bypassed because no real send occurs."
                    ),
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
        dry_run = bool(params.get("dry_run") or False)
        # Pyrogram accepts None for "no reply"; we coerce 0/missing to None.
        reply_to_raw = params.get("reply_to_message_id")
        try:
            reply_to_message_id: int | None = int(reply_to_raw) if reply_to_raw else None
        except (TypeError, ValueError):
            reply_to_message_id = None
        if reply_to_message_id is not None and reply_to_message_id <= 0:
            reply_to_message_id = None

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

        # Whitelist gate (non-empty list = gated). Enforced for dry_run too —
        # the agent shouldn't even *consider* drafting to non-whitelisted peers.
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

        if dry_run:
            self._audit.dry_run(
                recipient_id=recipient_id,
                recipient=label,
                text=text,
                reply_to_message_id=reply_to_message_id,
            )
            payload: dict[str, Any] = {
                "status": "ok",
                "dry_run": True,
                "would_send": True,
                "recipient": label,
                "text": text,
                "text_len": len(text),
            }
            if reply_to_message_id:
                payload["reply_to_message_id"] = reply_to_message_id
            return json.dumps(payload, ensure_ascii=False)

        # Rate limit gate (real sends only).
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
            send_kwargs: dict[str, Any] = {}
            if reply_to_message_id:
                send_kwargs["reply_to_message_id"] = reply_to_message_id
            msg = await client.send_message(entry["peer"], text, **send_kwargs)
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
            reply_to_message_id=reply_to_message_id,
        )

        # Preview post is best-effort and must NEVER raise back into the
        # tool result — the send already happened.
        await self._post_preview(text, label)

        result: dict[str, Any] = {
            "status": "ok",
            "ok": True,
            "message_id": message_id,
            "recipient": label,
        }
        if reply_to_message_id:
            result["reply_to_message_id"] = reply_to_message_id
        return json.dumps(result, ensure_ascii=False)

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
                date_str = self._format_date(getattr(m, "date", None))
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

    @tool(
        name="tg_scan_unread",
        description=(
            "Scan dialogs and return their recent messages in ONE call. "
            "Replaces the (list_recent_chats → get_chat_history × N) pattern "
            "for digest/triage workflows. Each dialog includes a fresh "
            "recipient_id token. Channels are excluded by default to keep "
            "broadcast noise out of the brief."
        ),
        parameters={
            "type": "object",
            "properties": {
                "max_dialogs": {
                    "type": "integer",
                    "description": "Max dialogs to inspect (1-50, default 20).",
                },
                "messages_per_dialog": {
                    "type": "integer",
                    "description": "Messages to fetch per dialog (1-30, default 10).",
                },
                "include_channels": {
                    "type": "boolean",
                    "description": "Include broadcast channels (default false).",
                },
                "include_groups": {
                    "type": "boolean",
                    "description": "Include groups/supergroups (default true).",
                },
                "only_unread": {
                    "type": "boolean",
                    "description": (
                        "If true, only return dialogs with unread_messages_count > 0 "
                        "(default true)."
                    ),
                },
            },
        },
    )
    async def tg_scan_unread(self, params: dict) -> str:
        client = await self._get_client()
        if client is None:
            return self._not_configured()

        max_dialogs = max(1, min(50, int(params.get("max_dialogs") or 20)))
        per_dialog = max(1, min(30, int(params.get("messages_per_dialog") or 10)))
        include_channels = bool(params.get("include_channels") or False)
        include_groups = (
            bool(params.get("include_groups")) if params.get("include_groups") is not None else True
        )
        only_unread = (
            bool(params.get("only_unread")) if params.get("only_unread") is not None else True
        )

        # First pass: collect candidate dialogs synchronously. We don't
        # parallelise the dialog iterator — pyrogram's get_dialogs is
        # already a single MTProto round-trip with paged results.
        candidates: list[tuple[Any, str, str | None, str, int, int]] = []
        try:
            async for dialog in client.get_dialogs(limit=max_dialogs):
                chat = getattr(dialog, "chat", None)
                if chat is None:
                    continue
                peer_type = self._chat_type(chat)
                if peer_type == "channel" and not include_channels:
                    continue
                if peer_type == "group" and not include_groups:
                    continue
                unread = int(getattr(dialog, "unread_messages_count", 0) or 0)
                if only_unread and unread <= 0:
                    continue
                username = getattr(chat, "username", None)
                title = (
                    getattr(chat, "title", None)
                    or getattr(chat, "first_name", None)
                    or (f"@{username}" if username else "")
                    or str(getattr(chat, "id", "?"))
                )
                peer_id = int(getattr(chat, "id", 0) or 0)
                candidates.append((chat, title, username, peer_type, peer_id, unread))
        except Exception as e:
            cls, friendly = self._friendly_rpc_error(e)
            return json.dumps(
                {"status": "error", "error_class": cls, "error": friendly},
                ensure_ascii=False,
            )

        # Mint tokens (under the lock once) before the parallel fetch so
        # the agent can immediately reply to anything it sees.
        peer_tokens: list[str] = []
        for chat, title, username, peer_type, peer_id, _ in candidates:
            token = await self._mint_token(
                peer=peer_id,
                username=username,
                first_name=title,
                peer_id=peer_id,
                peer_type=peer_type,
            )
            peer_tokens.append(token)

        # Parallel history fetch — this is where the speedup comes from.
        async def _fetch_history(peer_id: int) -> list[Any]:
            out: list[Any] = []
            try:
                async for m in client.get_chat_history(peer_id, limit=per_dialog):
                    out.append(m)
            except Exception:
                # Failures are isolated per-dialog; we still want the rest.
                return []
            return out

        histories = await asyncio.gather(
            *(_fetch_history(c[4]) for c in candidates),
            return_exceptions=False,
        )

        dialogs_out: list[dict[str, Any]] = []
        for (chat, title, _username, peer_type, _peer_id, unread), token, history in zip(
            candidates, peer_tokens, histories, strict=True,
        ):
            messages: list[dict[str, Any]] = []
            for m in history:
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
                date_str = self._format_date(getattr(m, "date", None))
                messages.append({
                    "from": sender_label,
                    "text": text,
                    "date": date_str,
                    "message_id": int(
                        getattr(m, "id", 0) or getattr(m, "message_id", 0) or 0,
                    ),
                })
            dialogs_out.append({
                "recipient_id": token,
                "title": title,
                "type": peer_type,
                "unread": unread,
                "messages": messages,
            })

        return json.dumps(
            {"status": "ok", "count": len(dialogs_out), "dialogs": dialogs_out},
            ensure_ascii=False,
        )

    @tool(
        name="tg_find_mentions",
        description=(
            "Find recent messages where the account owner was @-mentioned, "
            "tagged, or replied-to. Scans active dialogs over a lookback "
            "window. Returns mention context plus a recipient_id so the "
            "agent can reply or thread. Channels are excluded."
        ),
        parameters={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Lookback window in hours (1-168, default 24).",
                },
                "max_dialogs": {
                    "type": "integer",
                    "description": "Max dialogs to scan (1-100, default 30).",
                },
                "messages_per_dialog": {
                    "type": "integer",
                    "description": "Messages to inspect per dialog (1-100, default 30).",
                },
            },
        },
    )
    async def tg_find_mentions(self, params: dict) -> str:
        client = await self._get_client()
        if client is None:
            return self._not_configured()

        hours = max(1, min(168, int(params.get("hours") or 24)))
        max_dialogs = max(1, min(100, int(params.get("max_dialogs") or 30)))
        per_dialog = max(1, min(100, int(params.get("messages_per_dialog") or 30)))

        # Pull the account's @username for the substring fallback. pyrofork's
        # ``get_me`` returns a User with .username; if we can't get it we
        # still rely on the Telegram-set ``mentioned`` flag.
        my_username: str | None = None
        my_id: int | None = None
        try:
            me = await client.get_me()
            my_username = (getattr(me, "username", None) or "").lower() or None
            my_id = int(getattr(me, "id", 0) or 0) or None
        except Exception:
            pass

        cutoff_ts = time.time() - hours * 3600.0

        # Gather candidate dialogs first (groups + DMs only — channels are
        # broadcast noise where mentions usually mean nothing).
        candidates: list[tuple[Any, str, str | None, str, int]] = []
        try:
            async for dialog in client.get_dialogs(limit=max_dialogs):
                chat = getattr(dialog, "chat", None)
                if chat is None:
                    continue
                peer_type = self._chat_type(chat)
                if peer_type == "channel":
                    continue
                username = getattr(chat, "username", None)
                title = (
                    getattr(chat, "title", None)
                    or getattr(chat, "first_name", None)
                    or (f"@{username}" if username else "")
                    or str(getattr(chat, "id", "?"))
                )
                peer_id = int(getattr(chat, "id", 0) or 0)
                candidates.append((chat, title, username, peer_type, peer_id))
        except Exception as e:
            cls, friendly = self._friendly_rpc_error(e)
            return json.dumps(
                {"status": "error", "error_class": cls, "error": friendly},
                ensure_ascii=False,
            )

        async def _scan(peer_id: int) -> list[Any]:
            out: list[Any] = []
            try:
                async for m in client.get_chat_history(peer_id, limit=per_dialog):
                    date = getattr(m, "date", None)
                    ts = date.timestamp() if hasattr(date, "timestamp") else None
                    if ts is not None and ts < cutoff_ts:
                        # History is newest-first; once we cross the cutoff
                        # the rest is older. Stop early.
                        break
                    out.append(m)
            except Exception:
                return []
            return out

        histories = await asyncio.gather(
            *(_scan(c[4]) for c in candidates),
            return_exceptions=False,
        )

        mentions: list[dict[str, Any]] = []
        username_needle = f"@{my_username}" if my_username else None

        for (_chat, title, _username, peer_type, peer_id), history in zip(
            candidates, histories, strict=True,
        ):
            # Skip if this chat had nothing in the window.
            if not history:
                continue

            # Mint one token per chat that has a hit (lazily, after we
            # know there's a mention — saves token churn on dead chats).
            chat_token: str | None = None

            for m in history:
                text = getattr(m, "text", None) or getattr(m, "caption", None) or ""
                mentioned_flag = bool(getattr(m, "mentioned", False))

                # Reply-to-self: did the message reply to one of *my* messages?
                replied_to_self = False
                reply = getattr(m, "reply_to_message", None)
                if reply is not None and my_id is not None:
                    reply_from = getattr(reply, "from_user", None)
                    if reply_from is not None:
                        if int(getattr(reply_from, "id", 0) or 0) == my_id:
                            replied_to_self = True

                # Substring fallback (case-insensitive).
                substring_hit = False
                if username_needle and text:
                    substring_hit = username_needle in text.lower()

                if not (mentioned_flag or replied_to_self or substring_hit):
                    continue

                if chat_token is None:
                    chat_token = await self._mint_token(
                        peer=peer_id,
                        username=_username,
                        first_name=title,
                        peer_id=peer_id,
                        peer_type=peer_type,
                    )

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
                date_str = self._format_date(getattr(m, "date", None))

                mentions.append({
                    "recipient_id": chat_token,
                    "chat_title": title,
                    "chat_type": peer_type,
                    "from": sender_label,
                    "text": text or "<media>",
                    "message_id": int(
                        getattr(m, "id", 0) or getattr(m, "message_id", 0) or 0,
                    ),
                    "date": date_str,
                    "reason": (
                        "reply" if replied_to_self
                        else ("mention" if mentioned_flag else "substring")
                    ),
                })

        return json.dumps(
            {
                "status": "ok",
                "count": len(mentions),
                "lookback_hours": hours,
                "mentions": mentions,
            },
            ensure_ascii=False,
        )
