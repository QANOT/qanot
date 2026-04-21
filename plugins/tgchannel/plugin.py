"""Telegram channel automation — post, edit, delete, pin, schedule, cross-post.

The plugin works against channels where THIS bot is already an admin. The
owner adds the bot as admin once in Telegram's UI (Channel → Administrators
→ Add), then runs `channel_connect` from chat so the plugin records the
channel's id + title for later use.

State: workspace/memory/channels.json (persists across restarts).

Why a plugin and not prompting the agent directly:
  - The plugin holds the bot_token to make Bot API calls. The agent
    shouldn't be calling api.telegram.org with raw tokens in prompts.
  - The channel list needs to persist across turns/sessions — agent
    memory alone wouldn't cut it.
  - Deterministic resolution of user-provided names/IDs to numeric
    channel_ids, and bot-permission checks, live in code not prompts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from qanot.plugins.base import Plugin, ToolDef, tool

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(PLUGIN_DIR))


class TgChannelPlugin(Plugin):
    """Telegram channel manager for the bot's admin channels."""

    name = "tgchannel"
    description = "Post, edit, delete, pin and schedule to Telegram channels where the bot is admin"
    version = "0.1.0"

    def __init__(self) -> None:
        self._client: Any | None = None
        self._store: Any | None = None
        self._bot_id: int | None = None
        self._bot_username: str = ""
        self._workspace_dir: str = ""

    async def setup(self, config: dict) -> None:
        """Resolve the bot token + workspace dir and build the API client.

        We don't rely on a plugin-specific bot_token field. The plugin
        needs the SAME token the Telegram adapter uses for polling —
        i.e. the top-level ``Config.bot_token``. The loader hands us
        only plugin-scoped config, so we read the canonical config.json
        file directly via the ``QANOT_CONFIG`` env var (the same path
        the agent boots from).
        """
        import json as _json
        import os as _os

        cfg = config or {}
        core_token = ""
        workspace_dir = cfg.get("workspace_dir") or ""

        # 1) If the user (or platform) explicitly set bot_token in plugin
        #    config, respect it. Unusual — mainly a testing escape hatch.
        explicit = cfg.get("bot_token")
        if explicit:
            try:
                from qanot.secrets import resolve_secret

                core_token = resolve_secret(explicit) or ""
            except Exception:
                core_token = str(explicit)

        # 2) Otherwise read the canonical config.json the agent booted from.
        if not core_token:
            config_path = _os.environ.get("QANOT_CONFIG", "/data/config.json")
            try:
                if _os.path.exists(config_path):
                    raw = _json.loads(open(config_path, encoding="utf-8").read())
                    core_token = raw.get("bot_token") or ""
                    if not workspace_dir:
                        workspace_dir = raw.get("workspace_dir") or ""
            except Exception as e:
                logger.debug("tgchannel: couldn't read %s: %s", config_path, e)

        # 3) Last resort: env var override, useful for tests + local dev.
        if not core_token:
            core_token = _os.environ.get("QANOT_BOT_TOKEN", "") or ""

        if not core_token:
            logger.warning(
                "tgchannel plugin loaded WITHOUT a bot token — tools will "
                "return configuration errors. The plugin looks for the "
                "token in: plugin config, QANOT_CONFIG-pointed config.json, "
                "or QANOT_BOT_TOKEN env."
            )
            return

        try:
            from tg_engine.channels import ChannelStore
            from tg_engine.client import TelegramClient
        except Exception as e:
            logger.error("tgchannel engine import failed: %s", e)
            return

        if not workspace_dir:
            workspace_dir = "/data/workspace"
        self._workspace_dir = workspace_dir
        self._store = ChannelStore(workspace_dir)
        self._client = TelegramClient(core_token)

        # Probe bot identity once — we need the bot's user id to check its
        # admin status via getChatMember during channel_connect.
        try:
            me = await self._client.get_me()
            self._bot_id = int(me.get("id") or 0)
            self._bot_username = str(me.get("username") or "")
            logger.info(
                "tgchannel plugin ready — bot=@%s id=%d, %d channel(s) persisted",
                self._bot_username, self._bot_id, len(self._store.channels),
            )
        except Exception as e:
            logger.warning("tgchannel: getMe failed (token may be wrong): %s", e)

    async def teardown(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception as e:
                logger.debug("tgchannel client close failed: %s", e)

    def get_tools(self) -> list[ToolDef]:
        return self._collect_tools()

    # ── Helpers ──────────────────────────────────────────────────

    def _not_configured(self) -> str:
        return json.dumps(
            {
                "status": "unconfigured",
                "error": (
                    "Telegram kanal plugini ulanmagan — bot tokeni yo'q. "
                    "Plugin ro'yxatiga qayta qo'shing va bot-ni ishga tushiring."
                ),
            },
            ensure_ascii=False,
        )

    def _resolve_or_error(self, channel_id: Any) -> tuple[int | None, str | None]:
        """Resolve user-supplied channel reference to a numeric id.

        Returns (id, error_json). If id is None, error_json holds a
        JSON-serialised error the caller should return directly.
        """
        if self._store is None:
            return None, self._not_configured()
        cid = self._store.resolve(channel_id)
        if cid is None:
            return None, json.dumps(
                {
                    "status": "error",
                    "error": (
                        "Kanal topilmadi. Avval channel_connect orqali kanalni "
                        "qo'shing, yoki @username/numeric id bilan ishora qiling."
                    ),
                    "known_channels": self._store.list_all(),
                },
                ensure_ascii=False,
            )
        return cid, None

    # ── Tools ────────────────────────────────────────────────────

    @tool(
        name="channel_connect",
        description=(
            "Register a Telegram channel so the bot can post/edit/pin there. "
            "Pre-requisite: the bot MUST already be added as an admin in the "
            "channel with at least 'Post messages' permission. Accepts a "
            "@username, numeric channel_id (-100...), or invite link. Returns "
            "channel metadata + the bot's own permissions."
        ),
        parameters={
            "type": "object",
            "required": ["channel"],
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "@username, numeric id like -1001234567890, or https://t.me/channel",
                },
            },
        },
    )
    async def channel_connect(self, params: dict) -> str:
        if self._client is None or self._store is None:
            return self._not_configured()

        from tg_engine.errors import map_exception

        raw = (params.get("channel") or "").strip()
        if not raw:
            return json.dumps({"error": "channel is required"}, ensure_ascii=False)

        # Normalise: invite links + @username both supported by getChat
        ref: str | int = raw
        if raw.startswith("https://t.me/"):
            ref = "@" + raw.rsplit("/", 1)[-1].lstrip("+")
        elif not raw.startswith("@") and not raw.lstrip("-").isdigit():
            ref = "@" + raw

        try:
            chat = await self._client.get_chat(ref)
        except Exception as e:
            return json.dumps(
                {"status": "error", **map_exception(e)}, ensure_ascii=False,
            )

        # Verify bot's admin permissions
        permissions: dict[str, Any] = {}
        if self._bot_id and chat.get("id"):
            try:
                member = await self._client.get_chat_member(chat["id"], self._bot_id)
                status = member.get("status", "")
                permissions = {
                    "status": status,
                    "can_post_messages": member.get("can_post_messages"),
                    "can_edit_messages": member.get("can_edit_messages"),
                    "can_delete_messages": member.get("can_delete_messages"),
                    "can_pin_messages": member.get("can_pin_messages"),
                }
                if status not in ("administrator", "creator"):
                    return json.dumps(
                        {
                            "status": "error",
                            "error": (
                                "Bot bu kanalda admin emas. Kanal sozlamalari → "
                                "Administratorlar → bot-ni admin qilib qo'shing "
                                "va \"Post messages\" ruxsatini bering."
                            ),
                            "current_status": status,
                        },
                        ensure_ascii=False,
                    )
            except Exception as e:
                logger.debug("bot admin check failed: %s", e)

        added = self._store.add(chat)
        return json.dumps(
            {
                "status": "ok",
                "action": "added" if added else "updated",
                "channel": {
                    "id": chat.get("id"),
                    "title": chat.get("title"),
                    "username": chat.get("username"),
                    "type": chat.get("type"),
                },
                "bot_permissions": permissions,
                "total_channels": len(self._store.channels),
                "is_default": self._store.default_channel_id == chat.get("id"),
            },
            ensure_ascii=False,
        )

    @tool(
        name="channel_list",
        description=(
            "List all channels the bot is connected to (registered via "
            "channel_connect). Returns id, title, @username, and which one "
            "is the current default. Use this to discover channel IDs before "
            "posting."
        ),
        parameters={"type": "object", "properties": {}},
    )
    async def channel_list(self, params: dict) -> str:
        if self._store is None:
            return self._not_configured()
        channels = [
            {**c, "is_default": c["id"] == self._store.default_channel_id}
            for c in self._store.list_all()
        ]
        return json.dumps(
            {
                "status": "ok",
                "count": len(channels),
                "default_channel_id": self._store.default_channel_id,
                "channels": channels,
            },
            ensure_ascii=False,
        )

    @tool(
        name="channel_disconnect",
        description=(
            "Remove a channel from the bot's connected list. Does NOT remove "
            "the bot from the channel — that's a manual action in Telegram's "
            "admin list. After disconnecting, the bot still has admin access "
            "but won't see the channel in channel_list."
        ),
        parameters={
            "type": "object",
            "required": ["channel"],
            "properties": {
                "channel": {"type": "string", "description": "@username, id, or title"},
            },
        },
    )
    async def channel_disconnect(self, params: dict) -> str:
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err
        removed = self._store.remove(cid)  # type: ignore[arg-type]
        return json.dumps(
            {
                "status": "ok" if removed else "not_found",
                "channel_id": cid,
                "remaining": len(self._store.channels),
            },
            ensure_ascii=False,
        )

    @tool(
        name="channel_post",
        description=(
            "Post a message to a channel right now. Supports HTML parse mode "
            "(<b>, <i>, <a href>, <code>) and optional inline buttons (url "
            "or callback_data). Returns the new message_id so you can edit/"
            "pin/delete it later.\n\n"
            "IMPORTANT: always CONFIRM with the user before posting. Show "
            "them the final text + which channel, ask 'yuboraymi?' first."
        ),
        parameters={
            "type": "object",
            "required": ["text"],
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Channel @username, id, or title. Optional — defaults to default.",
                },
                "text": {"type": "string", "description": "Post body. HTML allowed."},
                "parse_mode": {
                    "type": "string",
                    "enum": ["HTML", "MarkdownV2", "none"],
                    "description": "Parse mode (default HTML). 'none' = plain text.",
                },
                "disable_web_page_preview": {
                    "type": "boolean",
                    "description": "Suppress the link preview when the post contains a URL.",
                },
                "buttons": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "url": {"type": "string"},
                                "callback_data": {"type": "string"},
                            },
                        },
                    },
                    "description": (
                        "2D array of inline-button rows. Each button needs text + "
                        "either url or callback_data. e.g. "
                        "[[{\"text\":\"Sotib olish\",\"url\":\"https://...\"}]]"
                    ),
                },
            },
        },
    )
    async def channel_post(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err

        text = params.get("text") or ""
        if not text.strip():
            return json.dumps({"error": "text is required"}, ensure_ascii=False)

        parse_mode = params.get("parse_mode")
        if parse_mode == "none":
            parse_mode = None
        elif parse_mode not in (None, "HTML", "MarkdownV2"):
            parse_mode = "HTML"
        elif parse_mode is None:
            parse_mode = "HTML"

        from tg_engine.client import make_inline_keyboard
        from tg_engine.errors import map_exception

        reply_markup = None
        if isinstance(params.get("buttons"), list) and params["buttons"]:
            reply_markup = make_inline_keyboard(params["buttons"])

        try:
            msg = await self._client.send_message(
                cid,  # type: ignore[arg-type]
                text,
                parse_mode=parse_mode,
                disable_web_page_preview=bool(
                    params.get("disable_web_page_preview", False),
                ),
                reply_markup=reply_markup,
            )
            return json.dumps(
                {
                    "status": "ok",
                    "channel_id": cid,
                    "message_id": msg.get("message_id"),
                    "url": _build_msg_url(cid, msg.get("message_id"), self._store),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("channel_post failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    @tool(
        name="channel_crosspost",
        description=(
            "Post the same message to multiple channels in parallel. If a single "
            "channel fails (bot not admin, rate-limited, etc), the rest still "
            "post — result is per-channel with ok/error flags. Pass 'all' to "
            "channels to post to every connected channel."
        ),
        parameters={
            "type": "object",
            "required": ["text", "channels"],
            "properties": {
                "channels": {
                    "oneOf": [
                        {"type": "string", "enum": ["all"]},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "\"all\" or array of channel refs (@username/id/title)",
                },
                "text": {"type": "string"},
                "parse_mode": {"type": "string", "enum": ["HTML", "MarkdownV2", "none"]},
                "disable_web_page_preview": {"type": "boolean"},
                "buttons": {"type": "array"},
            },
        },
    )
    async def channel_crosspost(self, params: dict) -> str:
        if self._client is None or self._store is None:
            return self._not_configured()

        from tg_engine.client import make_inline_keyboard
        from tg_engine.errors import map_exception

        text = params.get("text") or ""
        if not text.strip():
            return json.dumps({"error": "text is required"}, ensure_ascii=False)

        channels_in = params.get("channels")
        targets: list[int] = []
        if channels_in == "all":
            targets = [c["id"] for c in self._store.list_all()]
        elif isinstance(channels_in, list):
            for ref in channels_in:
                cid = self._store.resolve(ref)
                if cid is not None:
                    targets.append(cid)
        if not targets:
            return json.dumps(
                {"error": "no valid channels resolved"}, ensure_ascii=False,
            )
        targets = sorted(set(targets))

        parse_mode = params.get("parse_mode")
        if parse_mode == "none":
            parse_mode = None
        elif parse_mode is None:
            parse_mode = "HTML"

        reply_markup = None
        if isinstance(params.get("buttons"), list) and params["buttons"]:
            reply_markup = make_inline_keyboard(params["buttons"])

        async def _post_one(cid: int) -> dict:
            try:
                msg = await self._client.send_message(
                    cid,
                    text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=bool(
                        params.get("disable_web_page_preview", False),
                    ),
                    reply_markup=reply_markup,
                )
                return {
                    "channel_id": cid,
                    "ok": True,
                    "message_id": msg.get("message_id"),
                    "url": _build_msg_url(cid, msg.get("message_id"), self._store),
                }
            except Exception as e:
                return {"channel_id": cid, "ok": False, **map_exception(e)}

        results = await asyncio.gather(*[_post_one(cid) for cid in targets])
        success = sum(1 for r in results if r.get("ok"))
        return json.dumps(
            {
                "status": "ok" if success == len(results) else "partial" if success else "error",
                "success": success,
                "failed": len(results) - success,
                "total": len(results),
                "results": results,
            },
            ensure_ascii=False,
        )

    @tool(
        name="channel_edit",
        description=(
            "Edit an existing channel post (only text posts — photos/videos "
            "need a separate edit method). Telegram allows edits for 48h "
            "after posting; after that the API will refuse."
        ),
        parameters={
            "type": "object",
            "required": ["message_id", "text"],
            "properties": {
                "channel": {"type": "string"},
                "message_id": {"type": "integer"},
                "text": {"type": "string"},
                "parse_mode": {"type": "string", "enum": ["HTML", "MarkdownV2", "none"]},
                "disable_web_page_preview": {"type": "boolean"},
                "buttons": {"type": "array"},
            },
        },
    )
    async def channel_edit(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err
        try:
            message_id = int(params.get("message_id"))
        except (TypeError, ValueError):
            return json.dumps({"error": "message_id is required (integer)"}, ensure_ascii=False)
        text = params.get("text") or ""
        if not text.strip():
            return json.dumps({"error": "text is required"}, ensure_ascii=False)

        parse_mode = params.get("parse_mode")
        if parse_mode == "none":
            parse_mode = None
        elif parse_mode is None:
            parse_mode = "HTML"

        from tg_engine.client import make_inline_keyboard
        from tg_engine.errors import map_exception

        reply_markup = None
        if isinstance(params.get("buttons"), list) and params["buttons"]:
            reply_markup = make_inline_keyboard(params["buttons"])

        try:
            msg = await self._client.edit_message_text(
                cid,  # type: ignore[arg-type]
                message_id,
                text,
                parse_mode=parse_mode,
                disable_web_page_preview=bool(
                    params.get("disable_web_page_preview", False),
                ),
                reply_markup=reply_markup,
            )
            return json.dumps(
                {
                    "status": "ok",
                    "channel_id": cid,
                    "message_id": msg.get("message_id", message_id),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("channel_edit failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    @tool(
        name="channel_delete",
        description=(
            "Delete a channel post by message_id. Irreversible. Telegram "
            "allows deletion at any time for messages the bot posted."
        ),
        parameters={
            "type": "object",
            "required": ["message_id"],
            "properties": {
                "channel": {"type": "string"},
                "message_id": {"type": "integer"},
            },
        },
    )
    async def channel_delete(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err
        try:
            message_id = int(params.get("message_id"))
        except (TypeError, ValueError):
            return json.dumps({"error": "message_id is required (integer)"}, ensure_ascii=False)

        from tg_engine.errors import map_exception

        try:
            await self._client.delete_message(cid, message_id)  # type: ignore[arg-type]
            return json.dumps(
                {"status": "ok", "channel_id": cid, "message_id": message_id},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("channel_delete failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    @tool(
        name="channel_pin",
        description=(
            "Pin a message at the top of a channel. Silent by default (no "
            "notification to subscribers). Use disable_notification=false if "
            "you want Telegram to ping everyone."
        ),
        parameters={
            "type": "object",
            "required": ["message_id"],
            "properties": {
                "channel": {"type": "string"},
                "message_id": {"type": "integer"},
                "notify": {
                    "type": "boolean",
                    "description": "true = send notification to subscribers (default false = silent pin)",
                },
            },
        },
    )
    async def channel_pin(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err
        try:
            message_id = int(params.get("message_id"))
        except (TypeError, ValueError):
            return json.dumps({"error": "message_id is required"}, ensure_ascii=False)

        from tg_engine.errors import map_exception

        try:
            await self._client.pin_chat_message(
                cid,  # type: ignore[arg-type]
                message_id,
                disable_notification=not bool(params.get("notify", False)),
            )
            return json.dumps(
                {"status": "ok", "channel_id": cid, "pinned_message_id": message_id},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("channel_pin failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    @tool(
        name="channel_unpin",
        description=(
            "Unpin a specific message or (if message_id is omitted) the most "
            "recently pinned message."
        ),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "message_id": {"type": "integer"},
            },
        },
    )
    async def channel_unpin(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err
        mid = params.get("message_id")
        try:
            message_id = int(mid) if mid is not None else None
        except (TypeError, ValueError):
            message_id = None

        from tg_engine.errors import map_exception

        try:
            await self._client.unpin_chat_message(cid, message_id)  # type: ignore[arg-type]
            return json.dumps(
                {"status": "ok", "channel_id": cid, "unpinned_message_id": message_id},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("channel_unpin failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    @tool(
        name="channel_stats",
        description=(
            "Return basic channel statistics: subscriber count, bot's admin "
            "status, linked discussion group (if any). Real post-level view "
            "counts require the Telegram MTProto client API and aren't "
            "available via Bot API."
        ),
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
            },
        },
    )
    async def channel_stats(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err

        from tg_engine.errors import map_exception

        try:
            chat, count = await asyncio.gather(
                self._client.get_chat(cid),
                self._client.get_chat_member_count(cid),
                return_exceptions=False,
            )
            permissions: dict[str, Any] = {}
            if self._bot_id:
                try:
                    member = await self._client.get_chat_member(cid, self._bot_id)
                    permissions = {
                        "status": member.get("status"),
                        "can_post_messages": member.get("can_post_messages"),
                        "can_edit_messages": member.get("can_edit_messages"),
                        "can_delete_messages": member.get("can_delete_messages"),
                        "can_pin_messages": member.get("can_pin_messages"),
                    }
                except Exception:
                    pass
            return json.dumps(
                {
                    "status": "ok",
                    "id": chat.get("id"),
                    "title": chat.get("title"),
                    "username": chat.get("username"),
                    "type": chat.get("type"),
                    "description": chat.get("description") or "",
                    "subscriber_count": count,
                    "linked_chat_id": chat.get("linked_chat_id"),
                    "bot_permissions": permissions,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("channel_stats failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    # ── Media posting ────────────────────────────────────────────

    @tool(
        name="channel_post_photo",
        description=(
            "Post a photo to a channel with optional caption + inline buttons. "
            "Photo can be a public URL, a local file path in the bot's workspace, "
            "or a reusable Telegram file_id. Caption supports HTML formatting.\n\n"
            "ALWAYS confirm with the user before posting — show which channel, "
            "the photo source, and the caption, ask 'yuboraymi?' first."
        ),
        parameters={
            "type": "object",
            "required": ["photo"],
            "properties": {
                "channel": {"type": "string"},
                "photo": {
                    "type": "string",
                    "description": (
                        "Public image URL (https://...), local file path (/data/workspace/...), "
                        "or Telegram file_id from a previous upload"
                    ),
                },
                "caption": {"type": "string", "description": "Optional caption (max 1024 chars)"},
                "parse_mode": {"type": "string", "enum": ["HTML", "MarkdownV2", "none"]},
                "has_spoiler": {
                    "type": "boolean",
                    "description": "Hide image behind a spoiler blur (user taps to reveal)",
                },
                "show_caption_above_media": {
                    "type": "boolean",
                    "description": "Display caption above the photo instead of below",
                },
                "protect_content": {
                    "type": "boolean",
                    "description": "Forbid users from forwarding or saving the photo",
                },
                "notify": {"type": "boolean", "description": "Ping subscribers (default true)"},
                "buttons": {"type": "array"},
            },
        },
    )
    async def channel_post_photo(self, params: dict) -> str:
        return await self._post_single_media(params, method="photo", required_param="photo")

    @tool(
        name="channel_post_video",
        description=(
            "Post a video to a channel with optional caption + inline buttons. "
            "Video can be a public URL, local file path, or file_id. Videos are "
            "streamable by default (subscribers can start watching before the "
            "download completes)."
        ),
        parameters={
            "type": "object",
            "required": ["video"],
            "properties": {
                "channel": {"type": "string"},
                "video": {"type": "string"},
                "caption": {"type": "string"},
                "parse_mode": {"type": "string", "enum": ["HTML", "MarkdownV2", "none"]},
                "duration": {"type": "integer", "description": "Video duration in seconds"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "supports_streaming": {"type": "boolean", "description": "Default true"},
                "has_spoiler": {"type": "boolean"},
                "show_caption_above_media": {"type": "boolean"},
                "protect_content": {"type": "boolean"},
                "notify": {"type": "boolean"},
                "buttons": {"type": "array"},
            },
        },
    )
    async def channel_post_video(self, params: dict) -> str:
        return await self._post_single_media(params, method="video", required_param="video")

    @tool(
        name="channel_post_animation",
        description=(
            "Post a GIF or short animated video (soundless) with optional caption. "
            "Use this for memes, quick how-to GIFs, looping product shots."
        ),
        parameters={
            "type": "object",
            "required": ["animation"],
            "properties": {
                "channel": {"type": "string"},
                "animation": {"type": "string"},
                "caption": {"type": "string"},
                "parse_mode": {"type": "string", "enum": ["HTML", "MarkdownV2", "none"]},
                "has_spoiler": {"type": "boolean"},
                "show_caption_above_media": {"type": "boolean"},
                "protect_content": {"type": "boolean"},
                "notify": {"type": "boolean"},
                "buttons": {"type": "array"},
            },
        },
    )
    async def channel_post_animation(self, params: dict) -> str:
        return await self._post_single_media(params, method="animation", required_param="animation")

    @tool(
        name="channel_post_document",
        description=(
            "Post a file (PDF, DOCX, XLSX, ZIP, any type) with optional caption. "
            "Useful for catalogs, reports, price lists, contracts. File can be "
            "a URL, local path, or file_id. Max size via Bot API: 50MB."
        ),
        parameters={
            "type": "object",
            "required": ["document"],
            "properties": {
                "channel": {"type": "string"},
                "document": {"type": "string"},
                "caption": {"type": "string"},
                "parse_mode": {"type": "string", "enum": ["HTML", "MarkdownV2", "none"]},
                "protect_content": {"type": "boolean"},
                "notify": {"type": "boolean"},
                "buttons": {"type": "array"},
            },
        },
    )
    async def channel_post_document(self, params: dict) -> str:
        return await self._post_single_media(params, method="document", required_param="document")

    @tool(
        name="channel_post_audio",
        description=(
            "Post an audio track (MP3, M4A, etc.) with optional title/performer "
            "metadata and caption. Telegram displays it as a music player."
        ),
        parameters={
            "type": "object",
            "required": ["audio"],
            "properties": {
                "channel": {"type": "string"},
                "audio": {"type": "string"},
                "caption": {"type": "string"},
                "parse_mode": {"type": "string", "enum": ["HTML", "MarkdownV2", "none"]},
                "duration": {"type": "integer"},
                "performer": {"type": "string"},
                "title": {"type": "string"},
                "protect_content": {"type": "boolean"},
                "notify": {"type": "boolean"},
                "buttons": {"type": "array"},
            },
        },
    )
    async def channel_post_audio(self, params: dict) -> str:
        return await self._post_single_media(params, method="audio", required_param="audio")

    async def _post_single_media(
        self,
        params: dict,
        *,
        method: str,
        required_param: str,
    ) -> str:
        """Shared implementation for photo/video/document/audio/animation posts."""
        if self._client is None:
            return self._not_configured()
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err

        media = (params.get(required_param) or "").strip()
        if not media:
            return json.dumps(
                {"error": f"{required_param} is required"}, ensure_ascii=False,
            )

        parse_mode = params.get("parse_mode")
        if parse_mode == "none":
            parse_mode = None
        elif parse_mode is None:
            parse_mode = "HTML"

        from tg_engine.client import make_inline_keyboard
        from tg_engine.errors import map_exception

        reply_markup = None
        if isinstance(params.get("buttons"), list) and params["buttons"]:
            reply_markup = make_inline_keyboard(params["buttons"])

        caption = params.get("caption") or None
        if caption and len(caption) > 1024:
            return json.dumps(
                {
                    "error": (
                        f"Caption is {len(caption)} chars; Telegram limit for media is 1024. "
                        "Post without caption then send a separate text message, or use channel_post."
                    ),
                },
                ensure_ascii=False,
            )

        # Map `notify` flag (user-facing) → disable_notification (API)
        disable_notification = not bool(params.get("notify", True))

        try:
            send_fn = {
                "photo": self._client.send_photo,
                "video": self._client.send_video,
                "animation": self._client.send_animation,
                "document": self._client.send_document,
                "audio": self._client.send_audio,
            }[method]
        except KeyError:
            return json.dumps({"error": f"unknown media method: {method}"}, ensure_ascii=False)

        # Build per-method kwargs
        kwargs: dict[str, Any] = {
            "caption": caption,
            "parse_mode": parse_mode,
            "protect_content": bool(params.get("protect_content", False)),
            "disable_notification": disable_notification,
            "reply_markup": reply_markup,
        }
        if method in ("photo", "video", "animation"):
            kwargs["has_spoiler"] = bool(params.get("has_spoiler", False))
            kwargs["show_caption_above_media"] = bool(
                params.get("show_caption_above_media", False),
            )
        if method == "video":
            for k in ("duration", "width", "height"):
                v = params.get(k)
                if isinstance(v, (int, float)):
                    kwargs[k] = int(v)
            kwargs["supports_streaming"] = bool(
                params.get("supports_streaming", True),
            )
        if method == "audio":
            for k in ("duration",):
                v = params.get(k)
                if isinstance(v, (int, float)):
                    kwargs[k] = int(v)
            for k in ("performer", "title"):
                v = params.get(k)
                if v:
                    kwargs[k] = str(v)

        try:
            msg = await send_fn(cid, media, **kwargs)
            return json.dumps(
                {
                    "status": "ok",
                    "type": method,
                    "channel_id": cid,
                    "message_id": msg.get("message_id"),
                    "url": _build_msg_url(cid, msg.get("message_id"), self._store),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("channel_post_%s failed", method)
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    @tool(
        name="channel_post_album",
        description=(
            "Post an album of 2-10 photos and/or videos as a single grouped "
            "message. Great for product showcases, event photos, before/after "
            "comparisons. Only the FIRST item's caption is shown on the album "
            "(other captions are ignored by Telegram).\n\n"
            "Each media item is a dict: {type, media, caption?, parse_mode?}. "
            "Local file uploads within an album are NOT supported in v1 — "
            "pass public URLs or file_ids. For local files, post individually "
            "via channel_post_photo / channel_post_video."
        ),
        parameters={
            "type": "object",
            "required": ["media"],
            "properties": {
                "channel": {"type": "string"},
                "media": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["photo", "video", "document", "audio"],
                            },
                            "media": {"type": "string", "description": "URL or file_id"},
                            "caption": {"type": "string"},
                            "parse_mode": {"type": "string", "enum": ["HTML", "MarkdownV2"]},
                            "has_spoiler": {"type": "boolean"},
                        },
                        "required": ["type", "media"],
                    },
                },
                "protect_content": {"type": "boolean"},
                "notify": {"type": "boolean"},
            },
        },
    )
    async def channel_post_album(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err

        media = params.get("media") or []
        if not isinstance(media, list) or not (2 <= len(media) <= 10):
            return json.dumps(
                {"error": "media must be an array of 2-10 items"}, ensure_ascii=False,
            )

        from tg_engine.errors import map_exception

        # Normalise: default parse_mode, keep only supported fields
        cleaned: list[dict] = []
        for i, item in enumerate(media):
            if not isinstance(item, dict):
                return json.dumps(
                    {"error": f"media[{i}] is not an object"}, ensure_ascii=False,
                )
            entry: dict[str, Any] = {
                "type": item.get("type"),
                "media": item.get("media"),
            }
            if item.get("caption"):
                entry["caption"] = str(item["caption"])[:1024]
                entry["parse_mode"] = item.get("parse_mode") or "HTML"
            if "has_spoiler" in item and item["type"] in ("photo", "video"):
                entry["has_spoiler"] = bool(item["has_spoiler"])
            cleaned.append(entry)

        try:
            result = await self._client.send_media_group(
                cid,
                cleaned,
                disable_notification=not bool(params.get("notify", True)),
                protect_content=bool(params.get("protect_content", False)),
            )
            if isinstance(result, list):
                messages = result
            else:
                messages = [result] if result else []
            first_id = messages[0].get("message_id") if messages else None
            return json.dumps(
                {
                    "status": "ok",
                    "channel_id": cid,
                    "first_message_id": first_id,
                    "count": len(messages),
                    "message_ids": [m.get("message_id") for m in messages],
                    "url": _build_msg_url(cid, first_id, self._store),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("channel_post_album failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    @tool(
        name="channel_post_poll",
        description=(
            "Post a poll or quiz to a channel. Regular polls let subscribers "
            "vote freely; quizzes have a correct answer and show an explanation "
            "after the user answers.\n\n"
            "Use quiz mode for engagement content (trivia, product knowledge). "
            "Use regular polls for opinion gathering. Multi-answer is only "
            "available on regular polls."
        ),
        parameters={
            "type": "object",
            "required": ["question", "options"],
            "properties": {
                "channel": {"type": "string"},
                "question": {"type": "string", "description": "Poll question (1-300 chars)"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 10,
                    "description": "2-10 answer options (each 1-100 chars)",
                },
                "poll_type": {
                    "type": "string",
                    "enum": ["regular", "quiz"],
                    "description": "Default 'regular'",
                },
                "is_anonymous": {"type": "boolean", "description": "Default true"},
                "allows_multiple_answers": {
                    "type": "boolean",
                    "description": "Regular polls only. Default false.",
                },
                "correct_option_id": {
                    "type": "integer",
                    "description": "Quiz only — 0-based index of the correct answer in options",
                },
                "explanation": {
                    "type": "string",
                    "description": "Quiz only — shown after user answers (max 200 chars)",
                },
                "open_period": {
                    "type": "integer",
                    "description": "Auto-close after N seconds (5-600 for typical use, max 2628000)",
                },
                "protect_content": {"type": "boolean"},
                "notify": {"type": "boolean"},
            },
        },
    )
    async def channel_post_poll(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err

        from tg_engine.errors import map_exception

        question = (params.get("question") or "").strip()
        options = params.get("options") or []
        if not question or not isinstance(options, list) or len(options) < 2:
            return json.dumps(
                {"error": "question and at least 2 options are required"},
                ensure_ascii=False,
            )
        options = [str(o) for o in options[:10]]

        poll_type = params.get("poll_type") or "regular"

        try:
            result = await self._client.send_poll(
                cid,
                question,
                options,
                is_anonymous=bool(params.get("is_anonymous", True)),
                poll_type=poll_type,
                allows_multiple_answers=bool(
                    params.get("allows_multiple_answers", False),
                ),
                correct_option_id=params.get("correct_option_id"),
                explanation=params.get("explanation"),
                open_period=params.get("open_period"),
                protect_content=bool(params.get("protect_content", False)),
                disable_notification=not bool(params.get("notify", True)),
            )
            return json.dumps(
                {
                    "status": "ok",
                    "type": poll_type,
                    "channel_id": cid,
                    "message_id": result.get("message_id"),
                    "poll_id": (result.get("poll") or {}).get("id"),
                    "url": _build_msg_url(cid, result.get("message_id"), self._store),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("channel_post_poll failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    @tool(
        name="channel_stop_poll",
        description=(
            "Close an open poll before its natural end. The final results "
            "become visible immediately and no more votes are accepted. "
            "Returns the final Poll object with vote counts."
        ),
        parameters={
            "type": "object",
            "required": ["message_id"],
            "properties": {
                "channel": {"type": "string"},
                "message_id": {"type": "integer"},
            },
        },
    )
    async def channel_stop_poll(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err
        try:
            message_id = int(params.get("message_id"))
        except (TypeError, ValueError):
            return json.dumps({"error": "message_id is required"}, ensure_ascii=False)

        from tg_engine.errors import map_exception

        try:
            result = await self._client.stop_poll(cid, message_id)
            return json.dumps(
                {
                    "status": "ok",
                    "channel_id": cid,
                    "message_id": message_id,
                    "final_poll": result,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("channel_stop_poll failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    @tool(
        name="channel_edit_caption",
        description=(
            "Edit the caption of a photo/video/document/animation post. Use "
            "channel_edit for text posts (different API). 48h edit window "
            "after posting."
        ),
        parameters={
            "type": "object",
            "required": ["message_id", "caption"],
            "properties": {
                "channel": {"type": "string"},
                "message_id": {"type": "integer"},
                "caption": {"type": "string"},
                "parse_mode": {"type": "string", "enum": ["HTML", "MarkdownV2", "none"]},
                "show_caption_above_media": {"type": "boolean"},
                "buttons": {"type": "array"},
            },
        },
    )
    async def channel_edit_caption(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err
        try:
            message_id = int(params.get("message_id"))
        except (TypeError, ValueError):
            return json.dumps({"error": "message_id is required"}, ensure_ascii=False)

        caption = params.get("caption") or ""
        if len(caption) > 1024:
            return json.dumps(
                {"error": f"caption is {len(caption)} chars; limit 1024"}, ensure_ascii=False,
            )

        parse_mode = params.get("parse_mode")
        if parse_mode == "none":
            parse_mode = None
        elif parse_mode is None:
            parse_mode = "HTML"

        from tg_engine.client import make_inline_keyboard
        from tg_engine.errors import map_exception

        reply_markup = None
        if isinstance(params.get("buttons"), list) and params["buttons"]:
            reply_markup = make_inline_keyboard(params["buttons"])

        try:
            msg = await self._client.edit_message_caption(
                cid,
                message_id,
                caption,
                parse_mode=parse_mode,
                show_caption_above_media=bool(
                    params.get("show_caption_above_media", False),
                ),
                reply_markup=reply_markup,
            )
            return json.dumps(
                {"status": "ok", "channel_id": cid, "message_id": msg.get("message_id", message_id)},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("channel_edit_caption failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    @tool(
        name="channel_schedule",
        description=(
            "Schedule a post for a future time. Uses Qanot's cron scheduler "
            "under the hood — the scheduled prompt instructs the agent to "
            "call channel_post at the given time. Accepts a cron expression "
            "(e.g. '0 9 * * *' for every day at 09:00) or an absolute ISO "
            "datetime for one-shot.\n\n"
            "Returns the created cron job's name which the user can manage "
            "via cron_list / cron_delete."
        ),
        parameters={
            "type": "object",
            "required": ["text", "when"],
            "properties": {
                "channel": {"type": "string"},
                "text": {"type": "string"},
                "when": {
                    "type": "string",
                    "description": (
                        "Cron expression (5 fields) for recurring, or ISO "
                        "datetime (YYYY-MM-DDTHH:MM) for one-shot in "
                        "Asia/Tashkent timezone."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Optional name for the cron job (default auto-generated)",
                },
                "parse_mode": {"type": "string", "enum": ["HTML", "MarkdownV2", "none"]},
                "disable_web_page_preview": {"type": "boolean"},
            },
        },
    )
    async def channel_schedule(self, params: dict) -> str:
        if self._client is None or self._store is None:
            return self._not_configured()
        cid, err = self._resolve_or_error(params.get("channel"))
        if err:
            return err

        text = params.get("text") or ""
        when = (params.get("when") or "").strip()
        if not text.strip() or not when:
            return json.dumps(
                {"error": "text and when are required"}, ensure_ascii=False,
            )

        # We can't call cron_create directly (that's a core tool in the
        # agent's registry). Instead, we return a structured hand-off the
        # AGENT should complete by calling cron_create with the emitted
        # prompt. This keeps plugin-to-tool coupling clean.
        #
        # The prompt embeds the exact channel and text so the future cron
        # firing can re-run channel_post without ambiguity.
        parse_mode = params.get("parse_mode") or "HTML"
        if parse_mode == "none":
            parse_mode = None
        disable_preview = bool(params.get("disable_web_page_preview", False))

        prompt = (
            "channel_post tool-ini chaqiring:\n"
            f"- channel: {cid}\n"
            f"- text: {json.dumps(text, ensure_ascii=False)}\n"
            f"- parse_mode: {parse_mode or 'none'}\n"
            f"- disable_web_page_preview: {str(disable_preview).lower()}"
        )

        # Detect one-shot ISO datetime vs cron expression
        mode = "cron" if " " in when and when.count(" ") >= 4 else "once"

        return json.dumps(
            {
                "status": "ok",
                "action_required": "call cron_create with these parameters",
                "cron_create_params": {
                    "name": params.get("name") or f"channel_post_{cid}_{when.replace(' ', '_').replace(':', '')}",
                    "schedule": when,
                    "prompt": prompt,
                    "mode": "isolated",
                    "one_shot": mode == "once",
                },
                "channel_id": cid,
                "preview_text_head": text[:120] + ("…" if len(text) > 120 else ""),
            },
            ensure_ascii=False,
        )


# ── Module helpers ───────────────────────────────────────────────


def _build_msg_url(
    channel_id: int | None,
    message_id: int | None,
    store: Any,
) -> str | None:
    """Build a https://t.me/<username>/<message_id> URL when possible.

    Private channels (no username) don't have addressable URLs via Bot API
    alone, so we return None and the agent can mention 'no public URL'.
    """
    if not channel_id or not message_id or store is None:
        return None
    for c in store.list_all():
        if c.get("id") == channel_id and c.get("username"):
            return f"https://t.me/{c['username']}/{message_id}"
    # Private channel — use c/<id without -100 prefix>/<msg_id> format
    try:
        raw = abs(int(channel_id))
        if str(raw).startswith("100"):
            bare = str(raw)[3:]
            return f"https://t.me/c/{bare}/{message_id}"
    except (TypeError, ValueError):
        pass
    return None
