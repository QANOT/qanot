"""Telegram media handling — photo/sticker download, voice transcription, TTS."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message
    from qanot.config import Config
    from qanot.agent import Agent

logger = logging.getLogger(__name__)


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


async def download_photo(bot: "Bot", message: "Message") -> dict | None:
    """Download photo from Telegram, return Anthropic-style image block."""
    import base64
    from io import BytesIO

    if not message.photo:
        return None

    try:
        # Telegram provides multiple sizes, pick the largest
        photo = message.photo[-1]
        buf = BytesIO()
        await bot.download(photo, destination=buf)
        buf.seek(0)
        raw = buf.read()

        # Downscale if needed (handles oversized images safely)
        raw, media_type = _downscale_image(raw)

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


async def download_sticker(bot: "Bot", message: "Message") -> dict | str | None:
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
            await bot.download(sticker, destination=buf)
            buf.seek(0)
            raw = buf.read()
        elif sticker.thumbnail:
            # Animated/video sticker → use thumbnail (JPEG/WEBP)
            buf = BytesIO()
            await bot.download(sticker.thumbnail, destination=buf)
            buf.seek(0)
            raw = buf.read()

        if not raw:
            # No image available — return text-only description
            emoji = sticker.emoji or ""
            return f"[Sticker {emoji} (no preview available)]"

        # Small images, run through downscale for format normalization
        raw, media_type = _downscale_image(raw, max_dim=512)
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


async def transcribe_voice(bot: "Bot", message: "Message", config: "Config") -> str | None:
    """Download and transcribe a voice message or video note."""
    if not config.get_voice_api_key():
        logger.warning("Voice received but voice_api_key not configured")
        return None

    import tempfile
    from qanot.voice import (
        convert_ogg_to_mp3, convert_video_to_mp3, convert_video_to_ogg, transcribe,
    )

    provider = config.voice_provider
    audio_path = ""
    cleanup_paths: list[str] = []
    try:
        if message.voice:
            ogg_path = tempfile.mktemp(suffix=".ogg")
            await bot.download(message.voice, destination=ogg_path)
            cleanup_paths.append(ogg_path)
            logger.info("Voice downloaded: %ds", message.voice.duration)

            if provider in ("muxlisa", "whisper"):
                audio_path = ogg_path
            else:
                audio_path = await convert_ogg_to_mp3(ogg_path)
                cleanup_paths.append(audio_path)

        elif message.video_note:
            mp4_path = tempfile.mktemp(suffix=".mp4")
            await bot.download(message.video_note, destination=mp4_path)
            cleanup_paths.append(mp4_path)
            logger.info("Video note downloaded: %ds", message.video_note.duration)

            if provider in ("muxlisa", "whisper"):
                audio_path = await convert_video_to_ogg(mp4_path)
            else:
                audio_path = await convert_video_to_mp3(mp4_path)
            cleanup_paths.append(audio_path)
        else:
            return None

        result = await transcribe(
            audio_path,
            api_key=config.get_voice_api_key(),
            provider=provider,
            language=config.voice_language or None,
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


async def send_voice_reply(bot: "Bot", chat_id: int, user_id: str, agent: "Agent", config: "Config") -> None:
    """Send the last agent response as a TTS voice message."""
    from aiogram.enums import ChatAction
    from qanot.voice import text_to_speech, download_audio, convert_wav_to_ogg

    # Get the last assistant response from conversation
    conv = agent.get_conversation(user_id)
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
    async def voice_action_loop() -> None:
        try:
            while True:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
                await asyncio.sleep(4)
        except (asyncio.CancelledError, Exception):
            pass

    voice_typing = asyncio.create_task(voice_action_loop())

    provider = config.voice_provider
    cleanup_paths: list[str] = []
    try:
        result = await text_to_speech(
            last_text,
            api_key=config.get_voice_api_key(),
            provider=provider,
            language=config.voice_language or "uz",
            voice=config.voice_name or None,
        )

        voice_path = ""
        if result.audio_data:
            import tempfile
            wav_path = tempfile.mktemp(suffix=".wav")
            with open(wav_path, "wb") as f:
                f.write(result.audio_data)
            cleanup_paths.append(wav_path)

            voice_path = await convert_wav_to_ogg(wav_path)
            cleanup_paths.append(voice_path)

        elif result.audio_url:
            dl_path = await download_audio(result.audio_url)
            cleanup_paths.append(dl_path)
            if dl_path.endswith(".ogg"):
                voice_path = dl_path
            else:
                if dl_path.endswith(".wav"):
                    voice_path = await convert_wav_to_ogg(dl_path)
                else:
                    from qanot.voice import _run_ffmpeg
                    ogg_path = dl_path.rsplit(".", 1)[0] + ".ogg"
                    await _run_ffmpeg(
                        ["-i", dl_path, "-codec:a", "libopus", "-b:a", "32k", ogg_path, "-y"],
                        "ffmpeg audio→OGG",
                    )
                    voice_path = ogg_path
                cleanup_paths.append(voice_path)

        if not voice_path:
            return

        from aiogram.types import FSInputFile
        voice_file = FSInputFile(voice_path)
        await bot.send_voice(chat_id=chat_id, voice=voice_file)
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


async def send_pending_images(bot: "Bot", chat_id: int, user_id: str, agent: "Agent", *, thread_id: int | None = None) -> None:
    """Send any images generated by tools during this turn."""
    image_paths = agent.pop_pending_images(user_id)
    if not image_paths:
        return

    for path in image_paths:
        try:
            from aiogram.types import FSInputFile
            photo = FSInputFile(path)
            kwargs: dict = {"chat_id": chat_id, "photo": photo}
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            await bot.send_photo(**kwargs)
            logger.info("Sent generated image: %s", path)
        except Exception as e:
            logger.warning("Failed to send generated image %s: %s", path, e)


async def send_pending_files(bot: "Bot", chat_id: int, user_id: str, agent: "Agent", *, thread_id: int | None = None) -> None:
    """Send any files queued by send_file tool during this turn."""
    file_paths = agent.pop_pending_files(user_id)

    if not file_paths:
        return

    for path in file_paths:
        try:
            from aiogram.types import FSInputFile
            doc = FSInputFile(path)
            kwargs: dict = {"chat_id": chat_id, "document": doc}
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            await bot.send_document(**kwargs)
            logger.info("Sent file: %s", path)
        except Exception as e:
            logger.warning("Failed to send file %s: %s", path, e)
