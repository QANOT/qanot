"""Telegram adapter — aiogram 3.x long polling with configurable response modes."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.methods import SendMessageDraft, SetMessageReaction
from aiogram.types import BotCommand, Message, ReactionTypeEmoji
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

if TYPE_CHECKING:
    from qanot.agent import Agent
    from qanot.config import Config
    from qanot.scheduler import CronScheduler

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 4000

# Regex to strip leaked tool-call text (Llama models sometimes output these as text)
_TOOL_LEAK_RE = re.compile(
    r'<function[^>]*>.*?</function>|'
    r'<tool_call>.*?</tool_call>|'
    r'\{"name"\s*:\s*"[^"]+"\s*,\s*"parameters"\s*:',
    re.DOTALL,
)

# Pre-compiled regex patterns for _md_to_html (avoid recompilation per call)
_RE_CODE_BLOCK = re.compile(r"```(\w*)\n([\s\S]*?)```")
_RE_TABLE = re.compile(r"(?:^[|].*\n?)+", re.MULTILINE)
_RE_HR = re.compile(r"^---+$", re.MULTILINE)
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
_RE_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")


# ── Formatting helpers ──────────────────────────────────────


def _sanitize_response(text: str) -> str:
    """Strip leaked tool call artifacts from LLM output."""
    cleaned = _TOOL_LEAK_RE.sub("", text).strip()
    return cleaned if cleaned else text


def _md_to_html(text: str) -> str:
    """Convert agent markdown to Telegram HTML."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _RE_CODE_BLOCK.sub(r"<pre>\2</pre>", text)

    def wrap_table(m: re.Match) -> str:
        return f"\n<pre>{m.group(0).strip()}</pre>\n"
    text = _RE_TABLE.sub(wrap_table, text)
    text = _RE_HR.sub("\u2501" * 18, text)
    text = _RE_BOLD.sub(r"<b>\1</b>", text)
    text = _RE_INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _RE_HEADING.sub(r"<b>\1</b>", text)
    text = _RE_MULTI_NEWLINE.sub("\n\n", text)
    return text


def _split_text(text: str, limit: int = MAX_MSG_LEN) -> list[str]:
    """Split text into chunks respecting line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


# ── Adapter ─────────────────────────────────────────────────


class TelegramAdapter:
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
    ):
        self.config = config
        self.agent = agent
        self.scheduler = scheduler
        self.bot = Bot(token=config.bot_token)
        self.dp = Dispatcher()
        self._setup_handlers()
        self._concurrent = asyncio.Semaphore(config.max_concurrent)
        self._draft_counter = 0
        self._bot_username: str | None = None  # Cached bot username
        # Per-user message coalescing: when user sends rapid messages while
        # the agent is processing, collect them and process as one turn.
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._pending_messages: dict[str, list[tuple]] = {}

    def _next_draft_id(self) -> int:
        """Generate a unique draft_id for sendMessageDraft."""
        self._draft_counter += 1
        return self._draft_counter

    def _setup_handlers(self) -> None:
        # Command handlers (must be registered before generic F.text)
        @self.dp.message(F.text.startswith("/reset"))
        async def handle_reset(message: Message) -> None:
            await self._handle_reset(message)

        @self.dp.message(F.text.startswith("/status"))
        async def handle_status(message: Message) -> None:
            await self._handle_status(message)

        @self.dp.message(F.text.startswith("/help"))
        async def handle_help(message: Message) -> None:
            await self._handle_help(message)

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

    def _is_allowed(self, user_id: int) -> bool:
        if not self.config.allowed_users:
            return True
        return user_id in self.config.allowed_users

    async def _get_bot_username(self) -> str:
        """Get and cache the bot's username."""
        if self._bot_username is None:
            me = await self.bot.me()
            self._bot_username = me.username or ""
        return self._bot_username

    def _is_group_chat(self, message: Message) -> bool:
        """Check if the message is from a group or supergroup."""
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
            # Check if bot is @mentioned in text
            text = message.text or message.caption or ""
            if bot_username and f"@{bot_username}" in text:
                return True
            # Check if message is a reply to bot's own message
            if message.reply_to_message and message.reply_to_message.from_user:
                me = await self.bot.me()
                if message.reply_to_message.from_user.id == me.id:
                    return True
            return False
        return False

    def _strip_bot_mention(self, text: str, bot_username: str) -> str:
        """Remove @bot_mention from message text."""
        if not bot_username:
            return text
        return text.replace(f"@{bot_username}", "").strip()

    async def _handle_message(self, message: Message, *, is_voice: bool = False) -> None:
        if not message.from_user:
            return

        user_id = message.from_user.id
        if not self._is_allowed(user_id):
            return

        # Group chat handling
        is_group = self._is_group_chat(message)
        if is_group:
            if not await self._should_respond_in_group(message):
                return

        text = message.text or message.caption or ""
        voice_request = False

        # Voice message or video note → transcribe
        if is_voice and (message.voice or message.video_note):
            # Show action immediately so user knows bot is processing
            await self.bot.send_chat_action(
                chat_id=message.chat.id, action=ChatAction.TYPING,
            )
            transcript = await self._transcribe_voice(message)
            if transcript:
                text = f"{transcript} {text}".strip()
                voice_request = True
            else:
                await self._send_final(
                    message.chat.id,
                    "Ovozli xabarni qayta ishlab bo'lmadi. Iltimos, matn yozing.",
                )
                return

        # Photo → download, base64 encode for vision models
        images: list[dict] = []
        if message.photo:
            image_data = await self._download_photo(message)
            if image_data:
                images.append(image_data)
                if not text:
                    text = "Bu rasmni tahlil qiling."  # "Analyze this image"

        # Sticker → treat as conversational expression (like emoji), not image analysis
        if message.sticker:
            sticker_data = await self._download_sticker(message)
            if sticker_data:
                emoji = message.sticker.emoji or ""
                # Frame as a conversational reaction, not an image to analyze
                sticker_ctx = (
                    f"[The user sent a sticker {emoji}. "
                    f"Treat it as a conversational expression — react naturally like a human would. "
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
                text = f"[Document: {fname} — yuklab bo'lmadi] {text}".strip()

        # Reply-to-message context (message quoting + media)
        if message.reply_to_message:
            quoted = message.reply_to_message
            quoted_text = quoted.text or quoted.caption or ""
            # Truncate long quotes
            if len(quoted_text) > 1000:
                quoted_text = quoted_text[:1000] + "…"
            # Determine source
            quoted_from = "a message"
            if quoted.from_user:
                if quoted.from_user.is_bot:
                    quoted_from = "your previous message"
                else:
                    name = quoted.from_user.full_name or str(quoted.from_user.id)
                    quoted_from = f"a message from {name}"
            # Extract media from quoted message
            if quoted.photo and not images:
                quoted_img = await self._download_photo(quoted)
                if quoted_img:
                    images.append(quoted_img)
                    if not quoted_text:
                        quoted_text = "[image]"
            if quoted.sticker and not images:
                sticker_data = await self._download_sticker(quoted)
                if isinstance(sticker_data, dict) and sticker_data.get("type") == "image":
                    images.append(sticker_data)
                    emoji = quoted.sticker.emoji or ""
                    if not quoted_text:
                        quoted_text = f"[sticker {emoji}]"
            if quoted.voice and not voice_request:
                transcript = await self._transcribe_voice(quoted)
                if transcript:
                    quoted_text = f"{quoted_text} [voice: {transcript}]".strip()
            # Build reply annotation
            if quoted_text:
                text = f"[Replying to {quoted_from}: \"{quoted_text}\"]\n\n{text}"

        if not text:
            return

        # Group chat: strip bot mention and prefix sender name
        if is_group:
            bot_username = await self._get_bot_username()
            text = self._strip_bot_mention(text, bot_username)
            sender_name = message.from_user.full_name or str(user_id)
            text = f"[{sender_name}]: {text}"

        # Record user activity (helps heartbeat skip when user is active)
        if self.scheduler:
            self.scheduler.record_user_activity()

        # React with 👀 to acknowledge the message
        await self._react(message.chat.id, message.message_id, "👀")

        # For groups, use chat_id as coalescing key so all members share one conversation.
        # For DMs, use user_id.
        coalesce_key = f"group_{message.chat.id}" if is_group else str(user_id)

        # Add to pending buffer (before acquiring lock)
        self._pending_messages.setdefault(coalesce_key, []).append(
            (message, text, images, voice_request)
        )

        # Acquire lock — only one processor per conversation at a time.
        # If the lock is held (agent is processing), this handler waits.
        # When released, it drains ALL pending messages and coalesces them.
        lock = self._user_locks.setdefault(coalesce_key, asyncio.Lock())
        async with lock:
            batch = self._pending_messages.pop(coalesce_key, [])
            if not batch:
                return  # Already processed by a previous handler

            # Coalesce messages
            if len(batch) == 1:
                msg, text, images, voice_req = batch[0]
            else:
                texts = [t for _, t, _, _ in batch]
                text = "\n\n".join(texts)
                all_images: list[dict] = []
                for _, _, imgs, _ in batch:
                    if imgs:
                        all_images.extend(imgs)
                images = all_images or None
                msg = batch[-1][0]  # Use last message for reactions/chat_id
                voice_req = any(vr for _, _, _, vr in batch)
                logger.info(
                    "Coalesced %d messages into one turn (key=%s)",
                    len(batch), coalesce_key,
                )
                # React ✅ on earlier messages (they're included)
                for earlier_msg, _, _, _ in batch[:-1]:
                    await self._react(earlier_msg.chat.id, earlier_msg.message_id, "✅")

            # Only reply-to when messages were coalesced (multiple rapid messages)
            coalesced = len(batch) > 1
            async with self._concurrent:
                await self._process_turn(msg, coalesce_key, text, images, voice_req, coalesced=coalesced)

    async def _process_turn(
        self,
        message: Message,
        conv_key: str,
        text: str,
        images: list[dict] | None,
        voice_request: bool,
        *,
        coalesced: bool = False,
    ) -> None:
        """Process a single (possibly coalesced) turn for a conversation."""
        mode = self.config.response_mode
        # Reply-to based on config: off=never, coalesced=only batched, always=every message
        rm = self.config.reply_mode
        if rm == "always":
            reply_to = message.message_id
        elif rm == "coalesced" and coalesced:
            reply_to = message.message_id
        else:
            reply_to = None
        try:
            if mode == "stream":
                await self._respond_stream(message.chat.id, conv_key, text, images=images, reply_to=reply_to)
            elif mode == "partial":
                await self._respond_partial(message.chat.id, conv_key, text, images=images, reply_to=reply_to)
            else:
                await self._respond_blocked(message.chat.id, conv_key, text, images=images, reply_to=reply_to)

            # Send any generated images
            await self._send_pending_images(message.chat.id, conv_key)

            # Send TTS voice reply if configured
            should_tts = (
                self.config.voice_mode == "always"
                or (self.config.voice_mode == "inbound" and voice_request)
            )
            if should_tts and self.config.get_voice_api_key():
                await self._send_voice_reply(message.chat.id, conv_key)

            # React with ✅ when done
            await self._react(message.chat.id, message.message_id, "✅")

        except Exception as e:
            logger.error("Agent error: %s", e, exc_info=True)
            await self._react(message.chat.id, message.message_id, "❌")
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

    # ── Generated image delivery ────────────────────────────

    async def _send_pending_images(self, chat_id: int, user_id: str) -> None:
        """Send any images generated by tools during this turn."""
        image_paths = self.agent.pop_pending_images(user_id)
        if not image_paths:
            return

        for path in image_paths:
            try:
                from aiogram.types import FSInputFile
                photo = FSInputFile(path)
                await self.bot.send_photo(chat_id=chat_id, photo=photo)
                logger.info("Sent generated image: %s", path)
            except Exception as e:
                logger.warning("Failed to send generated image %s: %s", path, e)

    # ── Image processing ────────────────────────────────────

    @staticmethod
    def _downscale_image(raw: bytes, max_size: int = 4_000_000, max_dim: int = 1200) -> tuple[bytes, str]:
        """Always downscale images to save vision tokens.

        Claude charges ~1600 tokens for 1080x1080. Downscaling to 1200px max
        keeps quality good enough for analysis while saving context.
        Returns (image_bytes, media_type).
        """
        from PIL import Image
        from io import BytesIO

        img = Image.open(BytesIO(raw))
        w, h = img.size

        # Always resize if above max_dim (saves vision tokens)
        if max(w, h) > max_dim:
            ratio = max_dim / max(w, h)
            new_w, new_h = int(w * ratio), int(h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            logger.info("Image downscaled: %dx%d → %dx%d", w, h, new_w, new_h)
        elif len(raw) <= max_size:
            # Small image, no resize needed — detect MIME and return as-is
            if raw[:3] == b'\xff\xd8\xff':
                return raw, "image/jpeg"
            elif raw[:8] == b'\x89PNG\r\n\x1a\n':
                return raw, "image/png"
            elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
                return raw, "image/webp"
            elif raw[:3] == b'GIF':
                return raw, "image/gif"
            return raw, "image/jpeg"

        # Convert to JPEG (smallest size, vision models don't need PNG fidelity)
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        out = BytesIO()
        quality = 85
        img.save(out, format="JPEG", quality=quality)

        # If still too large, reduce quality
        while out.tell() > max_size and quality > 30:
            quality -= 15
            out = BytesIO()
            img.save(out, format="JPEG", quality=quality)

        result = out.getvalue()
        logger.info("Image compressed: %d → %d bytes (q=%d)", len(raw), len(result), quality)
        return result, "image/jpeg"

    async def _download_photo(self, message: Message) -> dict | None:
        """Download photo from Telegram, return Anthropic-style image block."""
        import base64
        from io import BytesIO

        if not message.photo:
            return None

        try:
            # Telegram provides multiple sizes, pick the largest
            photo = message.photo[-1]
            buf = BytesIO()
            await self.bot.download(photo, destination=buf)
            buf.seek(0)
            raw = buf.read()

            # Downscale if needed (handles oversized images safely)
            raw, media_type = self._downscale_image(raw)

            b64 = base64.b64encode(raw).decode("ascii")

            logger.info(
                "Photo downloaded: %d bytes, %s, %dx%d",
                len(raw), media_type, photo.width, photo.height,
            )

            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64,
                },
            }
        except Exception as e:
            logger.error("Photo download failed: %s", e)
            return None

    async def _download_sticker(self, message: Message) -> dict | str | None:
        """Download sticker and return image block for vision model.

        All sticker types use their thumbnail for vision analysis:
        - Static WEBP → download the sticker file directly
        - Animated (TGS) / Video (WEBM) → use the thumbnail image
        """
        sticker = message.sticker
        if not sticker:
            return None

        import base64
        from io import BytesIO

        try:
            raw: bytes | None = None

            if not sticker.is_animated and not sticker.is_video:
                # Static WEBP sticker → download directly
                buf = BytesIO()
                await self.bot.download(sticker, destination=buf)
                buf.seek(0)
                raw = buf.read()
            elif sticker.thumbnail:
                # Animated/video sticker → use thumbnail (JPEG/WEBP)
                buf = BytesIO()
                await self.bot.download(sticker.thumbnail, destination=buf)
                buf.seek(0)
                raw = buf.read()

            if not raw:
                # No image available — return text-only description
                emoji = sticker.emoji or ""
                return f"[Sticker {emoji} (no preview available)]"

            # Small images, run through downscale for format normalization
            raw, media_type = self._downscale_image(raw, max_dim=512)
            b64 = base64.b64encode(raw).decode("ascii")

            logger.info(
                "Sticker downloaded: %d bytes, %s, emoji=%s, set=%s, animated=%s",
                len(raw), media_type, sticker.emoji or "", sticker.set_name or "",
                sticker.is_animated or sticker.is_video,
            )

            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64,
                },
            }
        except Exception as e:
            logger.error("Sticker download failed: %s", e)
            return None

    # ── Telegram commands ────────────────────────────────────

    def _check_command_access(self, message: Message) -> tuple[int, str] | None:
        """Check command access and return (user_id, conv_key), or None if denied."""
        if not message.from_user:
            return None
        user_id = message.from_user.id
        if not self._is_allowed(user_id):
            return None
        conv_key = f"group_{message.chat.id}" if self._is_group_chat(message) else str(user_id)
        return user_id, conv_key

    async def _handle_reset(self, message: Message) -> None:
        """Handle /reset — clear conversation history."""
        access = self._check_command_access(message)
        if not access:
            return
        _, conv_key = access
        self.agent.reset(conv_key)
        await self._send_final(
            message.chat.id,
            "Suhbat tozalandi. Yangi suhbatni boshlashingiz mumkin.",
        )
        logger.info("Conversation reset: %s", conv_key)

    async def _handle_status(self, message: Message) -> None:
        """Handle /status — show session info."""
        access = self._check_command_access(message)
        if not access:
            return
        _, conv_key = access
        status = self.agent.context.session_status()
        conv = self.agent.get_conversation(conv_key)

        # Provider health info
        provider = self.agent.provider
        provider_info = f"Provider: {self.config.provider}\nModel: {self.config.model}"
        if hasattr(provider, "status"):
            lines = []
            for ps in provider.status():
                icon = "🟢" if ps["available"] else "🔴"
                active = " ◀" if ps["active"] else ""
                err = f" ({ps['last_error']})" if ps["last_error"] else ""
                lines.append(f"{icon} {ps['name']} — {ps['model']}{err}{active}")
            provider_info = "Providers:\n" + "\n".join(lines)

        status_text = (
            f"**Session Status**\n\n"
            f"Context: {status['context_percent']}%\n"
            f"Tokens: {status['total_tokens']:,}\n"
            f"Turns: {status['turn_count']}\n"
            f"Messages: {len(conv)}\n"
            f"Buffer: {'active' if status['buffer_active'] else 'inactive'}\n"
            f"{provider_info}"
        )
        await self._send_final(message.chat.id, status_text)

    async def _handle_help(self, message: Message) -> None:
        """Handle /help — show available commands."""
        if not self._check_command_access(message):
            return

        help_text = (
            "**Buyruqlar:**\n\n"
            "/reset — Suhbatni tozalash\n"
            "/status — Sessiya holati\n"
            "/help — Yordam\n\n"
            "Matn, rasm, sticker, ovozli xabar va fayllar qabul qilinadi."
        )
        await self._send_final(message.chat.id, help_text)

    async def _register_commands(self) -> None:
        """Register bot commands with BotFather (appears in Telegram UI menu)."""
        commands = [
            BotCommand(command="reset", description="Suhbatni tozalash"),
            BotCommand(command="status", description="Sessiya holati"),
            BotCommand(command="help", description="Yordam"),
        ]
        try:
            await self.bot.set_my_commands(commands)
            logger.info("Bot commands registered: %s", [c.command for c in commands])
        except Exception as e:
            logger.warning("Failed to register bot commands: %s", e)

    # ── Voice processing ────────────────────────────────────

    async def _transcribe_voice(self, message: Message) -> str | None:
        """Download and transcribe a voice message or video note."""
        if not self.config.get_voice_api_key():
            logger.warning("Voice received but voice_api_key not configured")
            return None

        import tempfile
        import os
        from qanot.voice import (
            convert_ogg_to_mp3, convert_video_to_mp3, convert_video_to_ogg, transcribe,
        )

        provider = self.config.voice_provider
        audio_path = ""
        cleanup_paths: list[str] = []
        try:
            if message.voice:
                ogg_path = tempfile.mktemp(suffix=".ogg")
                await self.bot.download(message.voice, destination=ogg_path)
                cleanup_paths.append(ogg_path)
                logger.info("Voice downloaded: %ds", message.voice.duration)

                if provider == "muxlisa":
                    # Muxlisa accepts OGG natively — no conversion needed!
                    audio_path = ogg_path
                else:
                    # KotibAI needs MP3
                    audio_path = await convert_ogg_to_mp3(ogg_path)
                    cleanup_paths.append(audio_path)

            elif message.video_note:
                mp4_path = tempfile.mktemp(suffix=".mp4")
                await self.bot.download(message.video_note, destination=mp4_path)
                cleanup_paths.append(mp4_path)
                logger.info("Video note downloaded: %ds", message.video_note.duration)

                if provider == "muxlisa":
                    audio_path = await convert_video_to_ogg(mp4_path)
                else:
                    audio_path = await convert_video_to_mp3(mp4_path)
                cleanup_paths.append(audio_path)
            else:
                return None

            result = await transcribe(
                audio_path,
                api_key=self.config.get_voice_api_key(),
                provider=provider,
                language=self.config.voice_language or None,
            )
            logger.info("Transcribed (%s): %s", provider, result.text[:100])
            return result.text

        except Exception as e:
            logger.error("Voice transcription failed: %s", e)
            return None
        finally:
            for path in cleanup_paths:
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    async def _send_voice_reply(self, chat_id: int, user_id: str) -> None:
        """Send the last agent response as a TTS voice message."""
        from qanot.voice import text_to_speech, download_audio, convert_wav_to_ogg
        import os

        # Get the last assistant response from conversation
        conv = self.agent.get_conversation(user_id)
        if not conv:
            return
        last_text = ""
        for msg in reversed(conv):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    last_text = content
                    break
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_text = block.get("text", "")
                            break
                if last_text:
                    break

        if not last_text or len(last_text) > 5000:
            return

        # Show "recording voice" action for better UX
        voice_typing = asyncio.create_task(self._voice_action_loop(chat_id))

        provider = self.config.voice_provider
        cleanup_paths: list[str] = []
        try:
            result = await text_to_speech(
                last_text,
                api_key=self.config.get_voice_api_key(),
                provider=provider,
                language=self.config.voice_language or "uz",
                voice=self.config.voice_name or None,
            )

            voice_path = ""
            if result.audio_data:
                # Muxlisa returns WAV bytes → save to temp → convert to OGG for Telegram
                import tempfile
                wav_path = tempfile.mktemp(suffix=".wav")
                with open(wav_path, "wb") as f:
                    f.write(result.audio_data)
                cleanup_paths.append(wav_path)

                voice_path = await convert_wav_to_ogg(wav_path)
                cleanup_paths.append(voice_path)

            elif result.audio_url:
                # KotibAI returns URL → download
                voice_path = await download_audio(result.audio_url)
                cleanup_paths.append(voice_path)

            if not voice_path:
                return

            from aiogram.types import FSInputFile
            voice_file = FSInputFile(voice_path)
            await self.bot.send_voice(chat_id=chat_id, voice=voice_file)
            logger.info("TTS voice reply sent (%s, %d chars)", provider, len(last_text))

        except Exception as e:
            logger.warning("TTS reply failed: %s", e)
        finally:
            voice_typing.cancel()
            for path in cleanup_paths:
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    # ── Response strategies ──────────────────────────────────

    async def _respond_stream(self, chat_id: int, user_id: str, text: str, *, images: list[dict] | None = None, reply_to: int | None = None) -> None:
        """Stream response via sendMessageDraft → sendMessage.

        Handles race conditions between draft updates and tool execution:
        - Pauses draft updates during tool execution to avoid conflicts
        - Tracks last sent draft text to avoid redundant updates
        - Only sends final message after draft is fully settled
        """
        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        draft_id = self._next_draft_id()
        accumulated = ""
        last_flush = 0.0
        last_sent_text = ""
        interval = self.config.stream_flush_interval
        drafting_paused = False

        try:
            async for event in self.agent.run_turn_stream(text, user_id=user_id, images=images, chat_id=chat_id):
                if event.type == "text_delta" and not drafting_paused:
                    accumulated += event.text
                    now = asyncio.get_event_loop().time()
                    if now - last_flush >= interval and accumulated != last_sent_text:
                        typing_task.cancel()
                        await self._send_draft(chat_id, draft_id, accumulated)
                        last_sent_text = accumulated
                        last_flush = now

                elif event.type == "text_delta" and drafting_paused:
                    # Tool iteration done, new text arriving — resume drafting
                    accumulated += event.text
                    drafting_paused = False

                elif event.type == "tool_use":
                    # Pause drafting during tool execution to avoid race
                    drafting_paused = True
                    if accumulated and accumulated != last_sent_text:
                        await self._send_draft(chat_id, draft_id, accumulated)
                        last_sent_text = accumulated
                    # Restart typing while tools execute
                    typing_task.cancel()
                    typing_task = asyncio.create_task(self._typing_loop(chat_id))

                elif event.type == "done":
                    break
        finally:
            typing_task.cancel()

        final_text = accumulated or "(No response)"
        await self._send_final(chat_id, final_text, reply_to=reply_to)

    async def _respond_partial(self, chat_id: int, user_id: str, text: str, *, images: list[dict] | None = None, reply_to: int | None = None) -> None:
        """Stream response via editMessageText (pre-9.5 fallback)."""
        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        accumulated = ""
        last_flush = 0.0
        interval = self.config.stream_flush_interval
        sent_msg_id: int | None = None

        try:
            async for event in self.agent.run_turn_stream(text, user_id=user_id, images=images, chat_id=chat_id):
                if event.type == "text_delta":
                    accumulated += event.text
                    now = asyncio.get_event_loop().time()
                    if now - last_flush >= interval and accumulated.strip():
                        if sent_msg_id is None:
                            try:
                                send_kwargs: dict = {"chat_id": chat_id, "text": accumulated[:MAX_MSG_LEN]}
                                if reply_to:
                                    send_kwargs["reply_to_message_id"] = reply_to
                                msg = await self.bot.send_message(**send_kwargs)
                                sent_msg_id = msg.message_id
                            except Exception as e:
                                logger.warning("Partial send failed: %s", e)
                        else:
                            try:
                                await self.bot.edit_message_text(
                                    chat_id=chat_id,
                                    message_id=sent_msg_id,
                                    text=accumulated[:MAX_MSG_LEN],
                                )
                            except Exception:
                                pass  # Edit failures are expected (unchanged text)
                        last_flush = now

                elif event.type == "done":
                    break
        finally:
            typing_task.cancel()

        # Final edit or send
        final_text = accumulated or "(No response)"
        if sent_msg_id:
            html = _md_to_html(final_text)
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id, message_id=sent_msg_id,
                    text=html[:MAX_MSG_LEN], parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            # Send remaining chunks if text exceeds limit
            if len(html) > MAX_MSG_LEN:
                for chunk in _split_text(html[MAX_MSG_LEN:]):
                    await self._send_final_chunk(chat_id, chunk)
        else:
            await self._send_final(chat_id, final_text, reply_to=reply_to)

    async def _respond_blocked(self, chat_id: int, user_id: str, text: str, *, images: list[dict] | None = None, reply_to: int | None = None) -> None:
        """Wait for full response, then send."""
        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        try:
            response = await self.agent.run_turn(text, user_id=user_id, images=images, chat_id=chat_id)
        finally:
            typing_task.cancel()
        await self._send_final(chat_id, response or "(No response)", reply_to=reply_to)

    # ── Low-level send methods ───────────────────────────────

    async def _send_draft(self, chat_id: int, draft_id: int, text: str) -> None:
        """Send a streaming draft via sendMessageDraft."""
        try:
            await self.bot(SendMessageDraft(
                chat_id=chat_id,
                draft_id=draft_id,
                text=text[:4096],
            ))
        except Exception as e:
            logger.debug("sendMessageDraft failed: %s", e)

    async def _send_final(self, chat_id: int, text: str, *, reply_to: int | None = None) -> None:
        """Send the final formatted message, splitting if needed."""
        if not text:
            return
        text = _sanitize_response(text)
        html = _md_to_html(text)
        chunks = _split_text(html)
        for i, chunk in enumerate(chunks):
            # Only reply_to on first chunk
            await self._send_final_chunk(chat_id, chunk, reply_to=reply_to if i == 0 else None)
            await asyncio.sleep(0.1)

    async def _send_final_chunk(self, chat_id: int, html_chunk: str, *, reply_to: int | None = None) -> None:
        """Send a single chunk with HTML fallback to plain text."""
        kwargs: dict = {"chat_id": chat_id, "text": html_chunk}
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to
        try:
            await self.bot.send_message(**kwargs, parse_mode=ParseMode.HTML)
        except Exception:
            try:
                await self.bot.send_message(**kwargs)
            except Exception as e:
                logger.error("Failed to send message: %s", e)

    async def send_message(self, chat_id: int, text: str) -> None:
        """Public method to send a message to a chat (used by sub-agents)."""
        await self._send_final(chat_id, text)

    async def _action_loop(self, chat_id: int, action: ChatAction = ChatAction.TYPING) -> None:
        """Send a chat action indicator every 4 seconds until cancelled."""
        try:
            while True:
                await self.bot.send_chat_action(chat_id=chat_id, action=action)
                await asyncio.sleep(4)
        except (asyncio.CancelledError, Exception):
            pass

    async def _typing_loop(self, chat_id: int) -> None:
        """Send typing indicator until cancelled."""
        await self._action_loop(chat_id, ChatAction.TYPING)

    async def _voice_action_loop(self, chat_id: int) -> None:
        """Send 'recording voice' indicator until cancelled."""
        await self._action_loop(chat_id, ChatAction.RECORD_VOICE)

    async def _react(self, chat_id: int, message_id: int, emoji: str) -> None:
        """Set a reaction emoji on a message. Silently fails if unsupported."""
        if not self.config.reactions_enabled:
            return
        try:
            await self.bot(SetMessageReaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            ))
        except Exception:
            pass  # Reactions may not be available in all chats

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
            logger.warning("No allowed_users configured — proactive message dropped")
            return

        if source:
            formatted = f"#agent #{source}\n{text}"
        else:
            formatted = f"#agent\n{text}"

        owner_id = self.config.allowed_users[0]
        try:
            await self._send_final(owner_id, formatted)
            logger.info("Proactive message delivered to owner %d", owner_id)
        except Exception as e:
            logger.warning("Failed to deliver proactive message to owner: %s", e)

    async def start(self) -> None:
        """Start the Telegram bot (polling or webhook based on config)."""
        logger.info(
            "[telegram] starting — transport=%s, response=%s, flush=%.1fs",
            self.config.telegram_mode,
            self.config.response_mode,
            self.config.stream_flush_interval,
        )
        await self._register_commands()
        asyncio.create_task(self._proactive_loop())

        if self.config.telegram_mode == "webhook" and self.config.webhook_url:
            await self._start_webhook()
        else:
            await self._start_polling()

    async def _start_polling(self) -> None:
        """Start with long polling."""
        try:
            await self.dp.start_polling(self.bot, drop_pending_updates=True)
        finally:
            await self.bot.session.close()

    async def _start_webhook(self) -> None:
        """Start with webhook via aiohttp server."""
        webhook_url = self.config.webhook_url.rstrip("/")
        webhook_path = "/webhook"
        full_url = f"{webhook_url}{webhook_path}"

        # Set webhook on Telegram side
        await self.bot.set_webhook(full_url, drop_pending_updates=True)
        logger.info("[telegram] webhook set: %s", full_url)

        # Create aiohttp app
        app = web.Application()
        handler = SimpleRequestHandler(dispatcher=self.dp, bot=self.bot)
        handler.register(app, path=webhook_path)
        setup_application(app, self.dp, bot=self.bot)

        # Run server
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.config.webhook_port)
        try:
            await site.start()
            logger.info("[telegram] webhook server listening on :%d", self.config.webhook_port)
            # Keep running until cancelled
            await asyncio.Event().wait()
        finally:
            await self.bot.delete_webhook()
            await runner.cleanup()
            await self.bot.session.close()
