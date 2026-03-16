"""Tests for TelegramAdapter sticker handling."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qanot.config import Config


# ── Helpers ──────────────────────────────────────────────────


def _make_sticker(*, is_animated=False, is_video=False, emoji="", thumbnail=None, set_name=""):
    """Create a mock Sticker object."""
    sticker = MagicMock()
    sticker.is_animated = is_animated
    sticker.is_video = is_video
    sticker.emoji = emoji
    sticker.thumbnail = thumbnail
    sticker.set_name = set_name
    sticker.file_id = "sticker_file_id"
    return sticker


def _make_message(*, user_id=12345, chat_id=67890, text=None, sticker=None, from_user=True):
    """Create a mock Message object."""
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.sticker = sticker
    msg.photo = None
    msg.document = None
    msg.voice = None
    msg.video_note = None
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.message_id = 999
    if from_user:
        msg.from_user = MagicMock()
        msg.from_user.id = user_id
    else:
        msg.from_user = None
    return msg


# ── Sticker Handling ─────────────────────────────────────────


class TestDownloadSticker:
    """Tests for TelegramAdapter._download_sticker."""

    @pytest.mark.asyncio
    async def test_static_sticker_downloads_directly(self):
        """Static WEBP stickers should be downloaded via bot.download."""
        from qanot.telegram import TelegramAdapter

        sticker = _make_sticker(is_animated=False, is_video=False, emoji="\U0001f600")
        message = _make_message(sticker=sticker)

        # Create a minimal WEBP-like payload (small enough to skip resize)
        fake_image = b'RIFF\x00\x00\x00\x00WEBP' + b'\x00' * 50

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.bot = AsyncMock()

        async def fake_download(file_obj, destination=None):
            if isinstance(destination, BytesIO):
                destination.write(fake_image)

        adapter.bot.download = fake_download

        with patch.object(TelegramAdapter, '_downscale_image', return_value=(fake_image, "image/webp")):
            result = await adapter._download_sticker(message)

        assert isinstance(result, dict)
        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/webp"

    @pytest.mark.asyncio
    async def test_animated_sticker_uses_thumbnail(self):
        """Animated TGS stickers should use the thumbnail image."""
        from qanot.telegram import TelegramAdapter

        thumbnail = MagicMock()
        sticker = _make_sticker(is_animated=True, is_video=False, emoji="\U0001f389", thumbnail=thumbnail)
        message = _make_message(sticker=sticker)

        fake_thumb = b'\xff\xd8\xff' + b'\x00' * 100  # JPEG-like

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.bot = AsyncMock()

        async def fake_download(file_obj, destination=None):
            if isinstance(destination, BytesIO):
                destination.write(fake_thumb)

        adapter.bot.download = fake_download

        with patch.object(TelegramAdapter, '_downscale_image', return_value=(fake_thumb, "image/jpeg")):
            result = await adapter._download_sticker(message)

        assert isinstance(result, dict)
        assert result["type"] == "image"
        assert result["source"]["media_type"] == "image/jpeg"
        # Verify thumbnail was passed to download, not the sticker itself
        # (bot.download was called with thumbnail object)

    @pytest.mark.asyncio
    async def test_video_sticker_uses_thumbnail(self):
        """Video WEBM stickers should use the thumbnail image."""
        from qanot.telegram import TelegramAdapter

        thumbnail = MagicMock()
        sticker = _make_sticker(is_animated=False, is_video=True, emoji="\U0001f525", thumbnail=thumbnail)
        message = _make_message(sticker=sticker)

        fake_thumb = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.bot = AsyncMock()

        async def fake_download(file_obj, destination=None):
            if isinstance(destination, BytesIO):
                destination.write(fake_thumb)

        adapter.bot.download = fake_download

        with patch.object(TelegramAdapter, '_downscale_image', return_value=(fake_thumb, "image/png")):
            result = await adapter._download_sticker(message)

        assert isinstance(result, dict)
        assert result["type"] == "image"

    @pytest.mark.asyncio
    async def test_animated_sticker_no_thumbnail_returns_text(self):
        """Animated stickers without thumbnail should return text description."""
        from qanot.telegram import TelegramAdapter

        sticker = _make_sticker(is_animated=True, is_video=False, emoji="\U0001f60e", thumbnail=None)
        message = _make_message(sticker=sticker)

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.bot = AsyncMock()

        result = await adapter._download_sticker(message)

        assert isinstance(result, str)
        assert "Sticker" in result
        assert "\U0001f60e" in result

    @pytest.mark.asyncio
    async def test_no_sticker_returns_none(self):
        """Message without sticker should return None."""
        from qanot.telegram import TelegramAdapter

        message = _make_message(sticker=None)
        message.sticker = None

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.bot = AsyncMock()

        result = await adapter._download_sticker(message)
        assert result is None

    @pytest.mark.asyncio
    async def test_download_error_returns_none(self):
        """Download failure should return None gracefully."""
        from qanot.telegram import TelegramAdapter

        sticker = _make_sticker(is_animated=False, is_video=False, emoji="\U0001f4a5")
        message = _make_message(sticker=sticker)

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.bot = AsyncMock()
        adapter.bot.download = AsyncMock(side_effect=Exception("network error"))

        result = await adapter._download_sticker(message)
        assert result is None
