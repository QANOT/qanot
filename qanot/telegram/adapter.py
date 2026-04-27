"""Core Telegram adapter — wires handlers, media, streaming into TelegramAdapter."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from qanot.telegram.formatting import _sanitize_response
from qanot.telegram.handlers import HandlersMixin
from qanot.telegram.media import (
    download_photo, download_sticker, send_pending_files,
    send_pending_images, send_pending_videos, send_voice_reply, transcribe_voice,
)
from qanot.telegram.streaming import StreamingMixin

if TYPE_CHECKING:
    from qanot.agent import Agent
    from qanot.config import Config
    from qanot.scheduler import CronScheduler

logger = logging.getLogger(__name__)


class TelegramAdapter(HandlersMixin, StreamingMixin):
    """Handles Telegram bot communication via aiogram long polling.

    Response modes (config.response_mode):
      - "stream":  Live streaming via sendMessageDraft (Bot API 9.5)
      - "partial": Periodic edits via editMessageText (fallback)
      - "blocked": Wait for full response, then send (simplest)
    """

    def __init__(
        self,
        config: "Config",
        agent: "Agent",
        scheduler: "CronScheduler | None" = None,
        subagent_manager=None,
    ):
        self.config = config
        self.agent = agent
        self.scheduler = scheduler
        self.subagent_manager = subagent_manager

        # Optional: point Bot at a self-hosted telegram-bot-api server for
        # 2GB file upload/download (vs 20MB on the public API).
        # Set TELEGRAM_API_URL env OR config.telegram_api_url to enable.
        import os as _os
        api_url = _os.environ.get("TELEGRAM_API_URL") or getattr(config, "telegram_api_url", None)
        if api_url:
            from aiogram.client.session.aiohttp import AiohttpSession
            from aiogram.client.telegram import TelegramAPIServer
            logger.info("Using self-hosted Bot API at %s (local mode)", api_url)
            # is_local=True tells aiogram that file_path is an absolute path
            # on our local filesystem — it reads the file directly instead of
            # HTTP-downloading. Required for the --local flag on telegram-bot-api.
            server = TelegramAPIServer.from_base(api_url, is_local=True)
            session = AiohttpSession(api=server)
            self.bot = Bot(token=config.bot_token, session=session)
        else:
            self.bot = Bot(token=config.bot_token)
        self.dp = Dispatcher()
        self._setup_handlers()
        self._concurrent = asyncio.Semaphore(config.max_concurrent)
        self._draft_counter = 0
        self._bot_username: str | None = None
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._pending_messages: dict[str, list[tuple]] = {}
        self._pending_approvals: dict[str, dict] = {}
        # MCP install/remove proposals awaiting user approval (10-min TTL, in-memory only)
        self._pending_mcp_proposals: dict[str, dict] = {}
        self._pending_mcp_removals: dict[str, dict] = {}
        # Config secret-set proposals awaiting user approval (10-min TTL, in-memory only)
        self._pending_config_proposals: dict[str, dict] = {}
        # Per-user: "I'm waiting for this provider's API key on your next message".
        # Populated by /voiceprovider tap on a key-less provider; consumed by
        # the next incoming text message from that user.
        self._pending_voice_key: dict[str, str] = {}
        from qanot.ratelimit import RateLimiter
        self._rate_limiter = RateLimiter()
        self.voicecall_manager = None  # Set by main.py if voicecall_enabled
        # Admin notification throttle: key -> last-sent monotonic time.
        self._admin_notify_last: dict[str, float] = {}

    async def notify_admins(self, text: str, throttle_key: str | None = None,
                            throttle_seconds: float = 3600.0) -> None:
        """Send a short alert to each admin_chat_ids. Throttled per key so
        recurring failures don't spam. Silently drops if no admin is set."""
        ids = getattr(self.config, "admin_chat_ids", None) or []
        if not ids:
            return
        if throttle_key is not None:
            import time as _time
            now = _time.monotonic()
            last = self._admin_notify_last.get(throttle_key, 0.0)
            if now - last < throttle_seconds:
                return
            self._admin_notify_last[throttle_key] = now
        for admin_id in ids:
            try:
                await self.bot.send_message(admin_id, text[:4000])
            except Exception as e:
                logger.warning("notify_admins to %s failed: %r", admin_id, e)

    def _setup_handlers(self) -> None:
        @self.dp.message(F.text == "/start")
        async def handle_start(message: Message) -> None:
            await self._handle_start(message)

        @self.dp.message(F.text.startswith("/reset"))
        async def handle_reset(message: Message) -> None:
            await self._handle_reset(message)

        @self.dp.message(F.text.startswith("/resume"))
        async def handle_resume(message: Message) -> None:
            await self._handle_resume(message)

        @self.dp.message(F.text.startswith("/status"))
        async def handle_status(message: Message) -> None:
            await self._handle_status(message)

        @self.dp.message(F.text.startswith("/help"))
        async def handle_help(message: Message) -> None:
            await self._handle_help(message)

        @self.dp.message(F.text.startswith("/model"))
        async def handle_model(message: Message) -> None:
            await self._handle_model(message)

        @self.dp.message(F.text.startswith("/think"))
        async def handle_think(message: Message) -> None:
            await self._handle_think(message)

        @self.dp.message(F.text.startswith("/voiceprovider"))
        async def handle_voiceprovider(message: Message) -> None:
            await self._handle_voiceprovider(message)

        @self.dp.message(F.text.startswith("/cancel_voice_key"))
        async def handle_cancel_voice_key(message: Message) -> None:
            await self._handle_cancel_voice_key(message)

        @self.dp.message(F.text.startswith("/voice"))
        async def handle_voice(message: Message) -> None:
            await self._handle_voice(message)

        @self.dp.message(F.text.startswith("/lang"))
        async def handle_lang(message: Message) -> None:
            await self._handle_lang(message)

        @self.dp.message(F.text.startswith("/mode"))
        async def handle_mode(message: Message) -> None:
            await self._handle_mode(message)

        @self.dp.message(F.text.startswith("/routing"))
        async def handle_routing(message: Message) -> None:
            await self._handle_routing(message)

        @self.dp.message(F.text.startswith("/group"))
        async def handle_group(message: Message) -> None:
            await self._handle_group(message)

        @self.dp.message(F.text.startswith("/topic"))
        async def handle_topic(message: Message) -> None:
            await self._handle_topic(message)

        @self.dp.message(F.text.startswith("/exec"))
        async def handle_exec(message: Message) -> None:
            await self._handle_exec(message)

        @self.dp.message(F.text.startswith("/code"))
        async def handle_code(message: Message) -> None:
            await self._handle_code(message)

        @self.dp.message(F.text.startswith("/context"))
        async def handle_context(message: Message) -> None:
            await self._handle_context(message)

        @self.dp.message(F.text.startswith("/usage"))
        async def handle_usage(message: Message) -> None:
            await self._handle_usage(message)

        @self.dp.message(F.text.startswith("/compact"))
        async def handle_compact(message: Message) -> None:
            await self._handle_compact(message)

        @self.dp.message(F.text.startswith("/export"))
        async def handle_export(message: Message) -> None:
            await self._handle_export(message)

        @self.dp.message(F.text.startswith("/id"))
        async def handle_id(message: Message) -> None:
            await self._handle_id(message)

        @self.dp.message(F.text.startswith("/joincall"))
        async def handle_joincall(message: Message) -> None:
            await self._handle_joincall(message)

        @self.dp.message(F.text.startswith("/leavecall"))
        async def handle_leavecall(message: Message) -> None:
            await self._handle_leavecall(message)

        @self.dp.message(F.text.startswith("/callstatus"))
        async def handle_callstatus(message: Message) -> None:
            await self._handle_callstatus(message)

        @self.dp.message(F.text.startswith("/stop"))
        async def handle_stop(message: Message) -> None:
            await self._handle_stop(message)

        @self.dp.message(F.text.startswith("/config"))
        async def handle_config(message: Message) -> None:
            await self._handle_config(message)

        @self.dp.message(F.text.startswith("/mcp"))
        async def handle_mcp(message: Message) -> None:
            await self._handle_mcp(message)

        @self.dp.message(F.text.startswith("/plugins"))
        async def handle_plugins(message: Message) -> None:
            await self._handle_plugins(message)

        @self.dp.message(F.text)
        async def handle_text(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.photo)
        async def handle_photo(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.sticker)
        async def handle_sticker(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.document)
        async def handle_document(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.voice)
        async def handle_voice(message: Message) -> None:
            await self._handle_message(message, is_voice=True)

        @self.dp.message(F.video_note)
        async def handle_video_note(message: Message) -> None:
            await self._handle_message(message, is_voice=True)

        @self.dp.message(F.video)
        async def handle_video(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.animation)
        async def handle_animation(message: Message) -> None:
            await self._handle_message(message)

        from aiogram.types import CallbackQuery

        @self.dp.callback_query()
        async def handle_callback(callback: CallbackQuery) -> None:
            await self._handle_callback_query(callback)

    def _is_allowed(self, user_id: int) -> bool:
        if not self.config.allowed_users:
            self.config.allowed_users = [user_id]
            self._save_owner(user_id)
            logger.info("Auto-owner: user %d is now the owner", user_id)
            return True
        return user_id in self.config.allowed_users

    def _save_owner(self, user_id: int) -> None:
        """Persist the auto-owner to config.json (atomic)."""
        try:
            from qanot.config import read_config_json, write_config_json
            raw = read_config_json()
            raw["allowed_users"] = [user_id]
            write_config_json(raw)
        except Exception as e:
            logger.warning("Failed to save auto-owner: %s", e)

    async def _get_bot_username(self) -> str:
        """Get and cache the bot's username."""
        if self._bot_username is None:
            me = await self.bot.me()
            self._bot_username = me.username or ""
        return self._bot_username

    def _is_group_chat(self, message: Message) -> bool:
        return message.chat.type in ("group", "supergroup")

    async def _should_respond_in_group(self, message: Message) -> bool:
        """Determine if the bot should respond to a group message."""
        mode = self.config.group_mode
        if mode == "off":
            return False
        if mode == "all":
            return True
        if mode == "mention":
            bot_username = await self._get_bot_username()
            text = message.text or message.caption or ""
            if bot_username and f"@{bot_username}" in text:
                return True
            if message.reply_to_message and message.reply_to_message.from_user:
                if message.reply_to_message.from_user.username == bot_username:
                    return True
            return False
        return False

    def _strip_bot_mention(self, text: str, bot_username: str) -> str:
        if not bot_username:
            return text
        return text.replace(f"@{bot_username}", "").strip()

    def _conv_key(self, message: Message) -> str:
        if not self._is_group_chat(message):
            return str(message.from_user.id)
        # Forum topics: isolate conversations per topic thread
        topic_id = getattr(message, "message_thread_id", None)
        if topic_id:
            return f"group_{message.chat.id}_topic_{topic_id}"
        return f"group_{message.chat.id}"

    def _check_command_access(self, message: Message) -> tuple[int, str] | None:
        if not message.from_user:
            return None
        user_id = message.from_user.id
        if not self._is_allowed(user_id):
            return None
        return user_id, self._conv_key(message)

    async def _handle_message(self, message: Message, *, is_voice: bool = False) -> None:
        if not message.from_user:
            return

        # Intercept: if this user is mid-flow entering a voice-provider API
        # key (tapped 🔒 on /voiceprovider), this message is the key — not
        # a prompt to the agent. Save it, don't forward to the LLM.
        if (
            not is_voice
            and message.text
            and str(message.from_user.id) in self._pending_voice_key
        ):
            try:
                if await self._handle_pending_voice_key(message):
                    return
            except Exception as e:
                logger.warning("pending voice-key handler failed: %s", e)

        # Group orchestration: route messages in the orchestration group
        if (
            self.config.group_orchestration
            and message.chat.id == self.config.orchestration_group_id
            and hasattr(self, "_group_orchestrator")
            and self._group_orchestrator
        ):
            try:
                handled = await self._group_orchestrator.route_message(message)
                if handled:
                    return
            except Exception as e:
                logger.warning("Group orchestrator routing failed: %s", e)
            # Fall through to default handling if not routed

        user_id_int = message.from_user.id
        if not self._is_allowed(user_id_int):
            return

        user_id = str(user_id_int)  # Convert once at Telegram boundary
        allowed, reason = self._rate_limiter.check(user_id)
        if not allowed:
            await message.reply(reason)
            return
        self._rate_limiter.record(user_id)

        is_group = self._is_group_chat(message)
        if is_group:
            # Always respond in bound topics (regardless of group_mode)
            thread_id = getattr(message, "message_thread_id", None)
            has_binding = bool(
                thread_id
                and self.config.topic_bindings.get(f"{message.chat.id}:{thread_id}")
            )
            if not has_binding and not await self._should_respond_in_group(message):
                return

        text = message.text or message.caption or ""
        voice_request = False

        if is_voice and (message.voice or message.video_note):
            await self.bot.send_chat_action(
                chat_id=message.chat.id, action=ChatAction.TYPING,
            )
            transcript = await transcribe_voice(self.bot, message, self.config)
            if transcript:
                text = f"{transcript} {text}".strip()
                voice_request = True
            else:
                await self._send_final(
                    message.chat.id,
                    "Ovozli xabarni qayta ishlab bo'lmadi. Iltimos, matn yozing.",
                )
                return

        images: list[dict] = []
        if message.photo:
            image_data = await download_photo(self.bot, message)
            if image_data:
                images.append(image_data)
                if not text:
                    text = "Bu rasmni tahlil qiling."

        if message.sticker:
            sticker_data = await download_sticker(self.bot, message)
            if sticker_data:
                emoji = message.sticker.emoji or ""
                sticker_ctx = (
                    f"[The user sent a sticker {emoji}. "
                    f"Treat it as a conversational expression \u2014 react naturally like a human would. "
                    f"Do NOT describe the image. Respond to the emotion/intent behind it.]"
                )
                if isinstance(sticker_data, dict) and sticker_data.get("type") == "image":
                    images.append(sticker_data)
                    text = f"{sticker_ctx} {text}".strip() if text else sticker_ctx
                elif isinstance(sticker_data, str):
                    text = f"{sticker_ctx} {text}".strip() if text else sticker_ctx

        if message.document:
            fname = message.document.file_name or "file"
            try:
                file = await self.bot.get_file(message.document.file_id)
                dl_dir = Path(self.config.workspace_dir) / "uploads"
                dl_dir.mkdir(parents=True, exist_ok=True)
                dl_path = dl_dir / fname
                await self.bot.download_file(file.file_path, dl_path)
                text = f"[Fayl yuklandi: uploads/{fname}] {text}".strip()
                logger.info("Downloaded file: %s", dl_path)
            except Exception as e:
                logger.error("File download failed: %s", e)
                text = f"[Document: {fname} \u2014 yuklab bo'lmadi] {text}".strip()

        # Video uploads (not document-type) — important for clipper plugin
        if message.video:
            try:
                file = await self.bot.get_file(message.video.file_id)
                dl_dir = Path(self.config.workspace_dir) / "uploads"
                dl_dir.mkdir(parents=True, exist_ok=True)
                ext = Path(file.file_path or "video.mp4").suffix or ".mp4"
                fname = f"{message.video.file_unique_id}{ext}"
                dl_path = dl_dir / fname

                expected_size = message.video.file_size or 0

                # For local Bot API mode: telegram-bot-api writes the file
                # asynchronously. aiogram's is_local=True reads whatever is
                # on disk NOW, which can return a partial file. Poll until
                # file size matches Telegram's reported size (or stabilizes).
                needs_wait = (
                    not dl_path.exists()
                    or (expected_size and dl_path.stat().st_size < expected_size)
                )
                if needs_wait:
                    await self.bot.download_file(file.file_path, dl_path)

                    # Wait for telegram-bot-api to finish writing (up to 5 min)
                    if expected_size:
                        deadline = asyncio.get_running_loop().time() + 300
                        while asyncio.get_running_loop().time() < deadline:
                            try:
                                # Read from the source (telegram-bot-api side)
                                if file.file_path and Path(file.file_path).exists():
                                    src_size = Path(file.file_path).stat().st_size
                                    if src_size >= expected_size:
                                        # Source complete — re-copy to be safe
                                        if dl_path.stat().st_size < src_size:
                                            await self.bot.download_file(file.file_path, dl_path)
                                        break
                                elif dl_path.exists() and dl_path.stat().st_size >= expected_size:
                                    break
                            except OSError:
                                pass
                            await asyncio.sleep(2)

                duration = message.video.duration or 0
                mm, ss = divmod(int(duration), 60)
                size_mb = dl_path.stat().st_size / 1_048_576 if dl_path.exists() else 0
                text = (
                    f"[Video yuklandi: {dl_path} "
                    f"({mm}:{ss:02d}, {message.video.width}x{message.video.height}, {size_mb:.0f}MB)] {text}"
                ).strip()
                logger.info("Downloaded video: %s (%ds, %.0fMB)", dl_path, duration, size_mb)
            except Exception as e:
                logger.error("Video download failed: %s", e, exc_info=True)
                text = f"[Video yuklab bo'lmadi: {e}] {text}".strip()

        if message.reply_to_message:
            quoted = message.reply_to_message
            quoted_text = quoted.text or quoted.caption or ""
            if len(quoted_text) > 1000:
                quoted_text = quoted_text[:1000] + "\u2026"
            quoted_from = "a message"
            if quoted.from_user:
                if quoted.from_user.is_bot:
                    quoted_from = "your previous message"
                else:
                    name = quoted.from_user.full_name or str(quoted.from_user.id)
                    quoted_from = f"a message from {name}"
            if quoted.photo and not images:
                quoted_img = await download_photo(self.bot, quoted)
                if quoted_img:
                    images.append(quoted_img)
                    if not quoted_text:
                        quoted_text = "[image]"
            if quoted.sticker and not images:
                sticker_data = await download_sticker(self.bot, quoted)
                if isinstance(sticker_data, dict) and sticker_data.get("type") == "image":
                    images.append(sticker_data)
                    emoji = quoted.sticker.emoji or ""
                    if not quoted_text:
                        quoted_text = f"[sticker {emoji}]"
            if quoted.voice and not voice_request:
                transcript = await transcribe_voice(self.bot, quoted, self.config)
                if transcript:
                    quoted_text = f"{quoted_text} [voice: {transcript}]".strip()
            if quoted_text:
                text = f"[Replying to {quoted_from}: \"{quoted_text}\"]\n\n{text}"

        if not text:
            return

        if is_group:
            bot_username = await self._get_bot_username()
            text = self._strip_bot_mention(text, bot_username)
            sender_name = message.from_user.full_name or str(user_id)
            text = f"[{sender_name}]: {text}"

        if self.scheduler:
            self.scheduler.record_user_activity()

        await self._react(message.chat.id, message.message_id, "\U0001f440")

        coalesce_key = self._conv_key(message)
        self._pending_messages.setdefault(coalesce_key, []).append(
            (message, text, images, voice_request)
        )

        lock = self._user_locks.setdefault(coalesce_key, asyncio.Lock())
        async with lock:
            batch = self._pending_messages.pop(coalesce_key, [])
            if not batch:
                return

            if len(batch) == 1:
                msg, text, images, voice_req = batch[0]
            else:
                text = "\n\n".join(t for _, t, _, _ in batch)
                images = [img for _, _, imgs, _ in batch if imgs for img in imgs] or None
                msg = batch[-1][0]
                voice_req = any(vr for _, _, _, vr in batch)
                logger.info(
                    "Coalesced %d messages into one turn (key=%s)",
                    len(batch), coalesce_key,
                )
                for earlier_msg, _, _, _ in batch[:-1]:
                    await self._react(earlier_msg.chat.id, earlier_msg.message_id, "\u2705")

            coalesced = len(batch) > 1
            async with self._concurrent:
                await self._process_turn(msg, coalesce_key, text, images, voice_req, coalesced=coalesced,
                                         thread_id=getattr(msg, "message_thread_id", None))

    def _resolve_topic_binding(self, chat_id: int, thread_id: int | None):
        """Resolve topic-agent binding. Returns AgentDefinition or None."""
        if not thread_id or not self.config.topic_bindings:
            return None
        binding_key = f"{chat_id}:{thread_id}"
        agent_id = self.config.topic_bindings.get(binding_key)
        if not agent_id:
            return None
        return next((ad for ad in self.config.agents if ad.id == agent_id), None)

    async def _process_turn(
        self,
        message: Message,
        conv_key: str,
        text: str,
        images: list[dict] | None,
        voice_request: bool,
        *,
        coalesced: bool = False,
        thread_id: int | None = None,
    ) -> None:
        """Process a single (possibly coalesced) turn for a conversation."""
        # Topic-agent binding: per-turn system prompt override (thread-safe)
        bound_agent = self._resolve_topic_binding(message.chat.id, thread_id)
        system_prompt_override: str | None = None
        if bound_agent and bound_agent.prompt:
            system_prompt_override = bound_agent.prompt
            logger.info("Topic binding active: %s → agent %s", conv_key, bound_agent.id)

        mode = self.config.response_mode
        rm = self.config.reply_mode
        if rm == "always":
            reply_to = message.message_id
        elif rm == "coalesced" and coalesced:
            reply_to = message.message_id
        else:
            reply_to = None
        try:
            if mode == "stream":
                await self._respond_stream(message.chat.id, conv_key, text, images=images, reply_to=reply_to, thread_id=thread_id, message_id=message.message_id, system_prompt_override=system_prompt_override)
            elif mode == "partial":
                await self._respond_partial(message.chat.id, conv_key, text, images=images, reply_to=reply_to, thread_id=thread_id, message_id=message.message_id, system_prompt_override=system_prompt_override)
            else:
                await self._respond_blocked(message.chat.id, conv_key, text, images=images, reply_to=reply_to, thread_id=thread_id, message_id=message.message_id, system_prompt_override=system_prompt_override)

            await send_pending_images(self.bot, message.chat.id, conv_key, self.agent, thread_id=thread_id)
            await send_pending_files(self.bot, message.chat.id, conv_key, self.agent, thread_id=thread_id)
            await send_pending_videos(self.bot, message.chat.id, conv_key, self.agent, thread_id=thread_id)

            should_tts = (
                self.config.voice_mode == "always"
                or (self.config.voice_mode == "inbound" and voice_request)
            )
            if should_tts and self.config.get_voice_api_key():
                await send_voice_reply(self.bot, message.chat.id, conv_key, self.agent, self.config)

            await self._react(message.chat.id, message.message_id, "\u2705")

        except Exception as e:
            logger.error("Agent error: %s", e, exc_info=True)
            await self._react(message.chat.id, message.message_id, "\u274c")
            err_msg = str(e).lower()
            if "rate_limit" in err_msg or "429" in err_msg:
                await self._send_final(
                    message.chat.id,
                    "Limitga yetdik. Iltimos, 20-30 soniya kutib qayta yozing.",
                )
            else:
                await self._send_final(
                    message.chat.id,
                    "Xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
                )

    # ── Proactive & lifecycle ────────────────────────────────

    async def _proactive_loop(self) -> None:
        if not self.scheduler:
            return
        while True:
            try:
                msg = await asyncio.wait_for(
                    self.scheduler.message_queue.get(), timeout=5.0,
                )
                msg_type = msg.get("type", "")
                text = msg.get("text", "")
                source = msg.get("source", "")
                if msg_type == "proactive" and text:
                    await self._deliver_proactive(text, source)
                elif msg_type == "system_event" and text:
                    await self.agent.run_turn(text)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("Proactive loop error: %s", e)
                await asyncio.sleep(5)

    async def _deliver_proactive(self, text: str, source: str = "") -> None:
        """Deliver a proactive message to the owner (first allowed user)."""
        if not self.config.allowed_users:
            logger.warning("No allowed_users configured \u2014 proactive message dropped")
            return

        source_tag = f" #{source}" if source else ""
        formatted = f"#agent{source_tag}\n{text}"

        owner_id = self.config.allowed_users[0]
        try:
            await self._send_final(owner_id, formatted)
            logger.info("Proactive message delivered to owner %d", owner_id)
        except Exception as e:
            logger.warning("Failed to deliver proactive message to owner: %s", e)

    async def start(self) -> None:
        """Start the Telegram bot (polling or webhook based on config)."""
        logger.info(
            "[telegram] starting \u2014 transport=%s, response=%s, flush=%.1fs",
            self.config.telegram_mode,
            self.config.response_mode,
            self.config.stream_flush_interval,
        )
        await self._register_commands()
        _proactive_task = asyncio.create_task(self._proactive_loop())
        _proactive_task.add_done_callback(
            lambda t: logger.warning("Proactive loop failed: %s", t.exception())
            if not t.cancelled() and t.exception() else None
        )

        if self.config.telegram_mode == "webhook" and self.config.webhook_url:
            await self._start_webhook()
        else:
            await self._start_polling()

    async def _start_polling(self) -> None:
        try:
            await self.dp.start_polling(self.bot, drop_pending_updates=True)
        finally:
            await self.bot.session.close()

    async def _start_webhook(self) -> None:
        webhook_url = self.config.webhook_url.rstrip("/")
        webhook_path = "/webhook"
        full_url = f"{webhook_url}{webhook_path}"

        await self.bot.set_webhook(full_url, drop_pending_updates=True)
        logger.info("[telegram] webhook set: %s", full_url)

        app = web.Application()
        handler = SimpleRequestHandler(dispatcher=self.dp, bot=self.bot)
        handler.register(app, path=webhook_path)
        setup_application(app, self.dp, bot=self.bot)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.config.webhook_port)
        try:
            await site.start()
            logger.info("[telegram] webhook server listening on :%d", self.config.webhook_port)
            await asyncio.Event().wait()
        finally:
            await self.bot.delete_webhook()
            await runner.cleanup()
            await self.bot.session.close()
