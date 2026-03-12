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


# ── Formatting helpers ──────────────────────────────────────


def _sanitize_response(text: str) -> str:
    """Strip leaked tool call artifacts from LLM output."""
    cleaned = _TOOL_LEAK_RE.sub("", text).strip()
    return cleaned if cleaned else text


def _md_to_html(text: str) -> str:
    """Convert agent markdown to Telegram HTML."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"```(\w*)\n([\s\S]*?)```", r"<pre>\2</pre>", text)

    def wrap_table(m: re.Match) -> str:
        return f"\n<pre>{m.group(0).strip()}</pre>\n"
    text = re.sub(r"(?:^[|].*\n?)+", wrap_table, text, flags=re.MULTILINE)
    text = re.sub(r"^---+$", "\u2501" * 18, text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
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

    async def _handle_message(self, message: Message, *, is_voice: bool = False) -> None:
        if not message.from_user:
            return

        user_id = message.from_user.id
        if not self._is_allowed(user_id):
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

        if not text:
            return

        # Record user activity (helps heartbeat skip when user is active)
        if self.scheduler:
            self.scheduler.record_user_activity()

        # React with 👀 to acknowledge the message
        await self._react(message.chat.id, message.message_id, "👀")

        async with self._concurrent:
            mode = self.config.response_mode
            try:
                if mode == "stream":
                    await self._respond_stream(message.chat.id, str(user_id), text, images=images)
                elif mode == "partial":
                    await self._respond_partial(message.chat.id, str(user_id), text, images=images)
                else:
                    await self._respond_blocked(message.chat.id, str(user_id), text, images=images)

                # Send TTS voice reply if configured
                should_tts = (
                    self.config.voice_mode == "always"
                    or (self.config.voice_mode == "inbound" and voice_request)
                )
                if should_tts and self.config.get_voice_api_key():
                    await self._send_voice_reply(message.chat.id, str(user_id))

                # React with ✅ when done
                await self._react(message.chat.id, message.message_id, "✅")

            except Exception as e:
                logger.error("Agent error: %s", e, exc_info=True)
                # React with ❌ on error
                await self._react(message.chat.id, message.message_id, "❌")
                # User-friendly error messages
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

    async def _handle_reset(self, message: Message) -> None:
        """Handle /reset — clear conversation history."""
        if not message.from_user:
            return
        user_id = message.from_user.id
        if not self._is_allowed(user_id):
            return

        self.agent.reset(str(user_id))
        await self._send_final(
            message.chat.id,
            "Suhbat tozalandi. Yangi suhbatni boshlashingiz mumkin.",
        )
        logger.info("Conversation reset for user %d", user_id)

    async def _handle_status(self, message: Message) -> None:
        """Handle /status — show session info."""
        if not message.from_user:
            return
        user_id = message.from_user.id
        if not self._is_allowed(user_id):
            return

        status = self.agent._context.session_status()
        conv = self.agent._conversations.get(str(user_id), [])

        status_text = (
            f"**Session Status**\n\n"
            f"Context: {status['context_percent']}%\n"
            f"Tokens: {status['total_tokens']:,}\n"
            f"Turns: {status['turn_count']}\n"
            f"Messages: {len(conv)}\n"
            f"Buffer: {'active' if status['buffer_active'] else 'inactive'}\n"
            f"Provider: {self.config.provider}\n"
            f"Model: {self.config.model}"
        )
        await self._send_final(message.chat.id, status_text)

    async def _handle_help(self, message: Message) -> None:
        """Handle /help — show available commands."""
        if not message.from_user:
            return
        if not self._is_allowed(message.from_user.id):
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
        conv = self.agent._conversations.get(user_id)
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

    async def _respond_stream(self, chat_id: int, user_id: str, text: str, *, images: list[dict] | None = None) -> None:
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
            async for event in self.agent.run_turn_stream(text, user_id=user_id, images=images):
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
        await self._send_final(chat_id, final_text)

    async def _respond_partial(self, chat_id: int, user_id: str, text: str, *, images: list[dict] | None = None) -> None:
        """Stream response via editMessageText (pre-9.5 fallback)."""
        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        accumulated = ""
        last_flush = 0.0
        interval = self.config.stream_flush_interval
        sent_msg_id: int | None = None

        async for event in self.agent.run_turn_stream(text, user_id=user_id, images=images):
            if event.type == "text_delta":
                accumulated += event.text
                now = asyncio.get_event_loop().time()
                if now - last_flush >= interval and accumulated.strip():
                    if sent_msg_id is None:
                        try:
                            msg = await self.bot.send_message(
                                chat_id=chat_id, text=accumulated[:MAX_MSG_LEN],
                            )
                            sent_msg_id = msg.message_id
                        except Exception:
                            pass
                    else:
                        try:
                            await self.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=sent_msg_id,
                                text=accumulated[:MAX_MSG_LEN],
                            )
                        except Exception:
                            pass
                    last_flush = now

            elif event.type == "done":
                break

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
            await self._send_final(chat_id, final_text)

    async def _respond_blocked(self, chat_id: int, user_id: str, text: str, *, images: list[dict] | None = None) -> None:
        """Wait for full response, then send."""
        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        try:
            response = await self.agent.run_turn(text, user_id=user_id, images=images)
        finally:
            typing_task.cancel()
        await self._send_final(chat_id, response or "(No response)")

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

    async def _send_final(self, chat_id: int, text: str) -> None:
        """Send the final formatted message, splitting if needed."""
        if not text:
            return
        text = _sanitize_response(text)
        html = _md_to_html(text)
        for chunk in _split_text(html):
            await self._send_final_chunk(chat_id, chunk)
            await asyncio.sleep(0.1)

    async def _send_final_chunk(self, chat_id: int, html_chunk: str) -> None:
        """Send a single chunk with HTML fallback to plain text."""
        try:
            await self.bot.send_message(
                chat_id=chat_id, text=html_chunk, parse_mode=ParseMode.HTML,
            )
        except Exception:
            try:
                await self.bot.send_message(chat_id=chat_id, text=html_chunk)
            except Exception as e:
                logger.error("Failed to send message: %s", e)

    async def _typing_loop(self, chat_id: int) -> None:
        """Send typing indicator every 4 seconds until cancelled."""
        try:
            while True:
                await self.bot.send_chat_action(
                    chat_id=chat_id, action=ChatAction.TYPING,
                )
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _voice_action_loop(self, chat_id: int) -> None:
        """Send 'recording voice' indicator every 4 seconds until cancelled."""
        try:
            while True:
                await self.bot.send_chat_action(
                    chat_id=chat_id, action=ChatAction.RECORD_VOICE,
                )
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _react(self, chat_id: int, message_id: int, emoji: str) -> None:
        """Set a reaction emoji on a message. Silently fails if unsupported."""
        try:
            await self.bot(SetMessageReaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            ))
        except Exception:
            pass  # Reactions may not be available in all chats

    async def _clear_reaction(self, chat_id: int, message_id: int) -> None:
        """Remove all reactions from a message."""
        try:
            await self.bot(SetMessageReaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[],
            ))
        except Exception:
            pass

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
