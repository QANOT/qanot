"""Tests for qanot.voice — KotibAI STT/TTS integration."""

import asyncio
import json
import os
import struct
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qanot.voice import (
    KOTIB_BASE_URL,
    VOICES,
    DEFAULT_VOICE,
    TranscriptionResult,
    TTSResult,
    convert_ogg_to_mp3,
    convert_video_to_mp3,
    transcribe,
    text_to_speech,
    download_audio,
)


# ── Unit tests ────────────────────────────────────────────


class TestVoiceDataClasses:
    def test_transcription_result(self):
        r = TranscriptionResult(text="salom", language="uz")
        assert r.text == "salom"
        assert r.language == "uz"

    def test_transcription_result_no_language(self):
        r = TranscriptionResult(text="hello")
        assert r.language is None

    def test_tts_result(self):
        r = TTSResult(audio_url="https://example.com/audio.mp3", character_count=42)
        assert r.audio_url == "https://example.com/audio.mp3"
        assert r.character_count == 42

    def test_tts_result_defaults(self):
        r = TTSResult(audio_url="https://example.com/audio.mp3")
        assert r.character_count == 0


class TestVoiceConstants:
    def test_voices_dict_has_all_entries(self):
        assert len(VOICES) == 6
        assert "aziza" in VOICES
        assert "sherzod" in VOICES
        assert "rachel" in VOICES
        assert "arnold" in VOICES

    def test_default_voice(self):
        assert DEFAULT_VOICE == "aziza"
        assert DEFAULT_VOICE in VOICES

    def test_kotib_base_url(self):
        assert "kotib.ai" in KOTIB_BASE_URL
        assert KOTIB_BASE_URL.startswith("https://")


# ── FFmpeg conversion tests (mock subprocess) ────────────


class TestAudioConversion:
    def test_ogg_to_mp3_success(self):
        with patch("qanot.voice.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc

            result = asyncio.run(convert_ogg_to_mp3("/tmp/test.ogg"))
            assert result == "/tmp/test.mp3"

            # Verify ffmpeg was called with correct args
            call_args = mock_exec.call_args[0]
            assert call_args[0] == "ffmpeg"
            assert "-i" in call_args
            assert "/tmp/test.ogg" in call_args

    def test_ogg_to_mp3_failure(self):
        with patch("qanot.voice.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.communicate = AsyncMock(return_value=(b"", b"error message"))
            mock_exec.return_value = mock_proc

            with pytest.raises(RuntimeError, match="ffmpeg conversion failed"):
                asyncio.run(convert_ogg_to_mp3("/tmp/test.ogg"))

    def test_video_to_mp3_success(self):
        with patch("qanot.voice.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc

            result = asyncio.run(convert_video_to_mp3("/tmp/note.mp4"))
            assert result == "/tmp/note.mp3"

            call_args = mock_exec.call_args[0]
            assert "-vn" in call_args  # video stream discarded

    def test_video_to_mp3_failure(self):
        with patch("qanot.voice.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.communicate = AsyncMock(return_value=(b"", b"codec error"))
            mock_exec.return_value = mock_proc

            with pytest.raises(RuntimeError, match="ffmpeg extraction failed"):
                asyncio.run(convert_video_to_mp3("/tmp/note.mp4"))


# ── KotibAI STT tests (mock HTTP) ────────────────────────


class TestTranscribe:
    def _mock_response(self, status=200, json_data=None):
        resp = AsyncMock()
        resp.status = status
        resp.json = AsyncMock(return_value=json_data or {})
        return resp

    def test_transcribe_success(self):
        response_data = {"status": "success", "text": "Salom dunyo"}

        with patch("qanot.voice.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_resp = self._mock_response(200, response_data)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            # Create a temp file to read
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.write(b"fake audio data")
            tmp.close()

            try:
                result = asyncio.run(transcribe(tmp.name, api_key="test-key"))
                assert result.text == "Salom dunyo"
            finally:
                os.unlink(tmp.name)

    def test_transcribe_with_language(self):
        response_data = {"status": "success", "text": "Hello world"}

        with patch("qanot.voice.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_resp = self._mock_response(200, response_data)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.write(b"fake")
            tmp.close()

            try:
                result = asyncio.run(transcribe(tmp.name, api_key="k", language="en"))
                assert result.text == "Hello world"
                assert result.language == "en"
            finally:
                os.unlink(tmp.name)

    def test_transcribe_api_error(self):
        with patch("qanot.voice.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_resp = self._mock_response(401, {"error": "Invalid API key"})
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.write(b"fake")
            tmp.close()

            try:
                with pytest.raises(RuntimeError, match="KotibAI STT error"):
                    asyncio.run(transcribe(tmp.name, api_key="bad-key"))
            finally:
                os.unlink(tmp.name)

    def test_transcribe_non_success_status(self):
        with patch("qanot.voice.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_resp = self._mock_response(200, {"status": "failed", "error": "bad audio"})
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.write(b"fake")
            tmp.close()

            try:
                with pytest.raises(RuntimeError, match="KotibAI STT failed"):
                    asyncio.run(transcribe(tmp.name, api_key="k"))
            finally:
                os.unlink(tmp.name)


# ── KotibAI TTS tests (mock HTTP) ────────────────────────


class TestTextToSpeech:
    def test_tts_short_text_blocking(self):
        """Short text (<500 chars) uses blocking=true."""
        response_data = {
            "status": "success",
            "audio_url": "https://developer.kotib.ai/media/tasks/tts_123.mp3",
            "character_count": 12,
        }

        with patch("qanot.voice.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value=response_data)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            result = asyncio.run(text_to_speech("Salom dunyo", api_key="k"))
            assert result.audio_url == response_data["audio_url"]
            assert result.character_count == 12

    def test_tts_default_voice_uzbek(self):
        """Default voice for Uzbek is Aziza."""
        response_data = {"status": "success", "audio_url": "https://x.com/a.mp3"}

        with patch("qanot.voice.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value=response_data)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            asyncio.run(text_to_speech("test", api_key="k", language="uz"))

            # Check the payload sent
            call_args = mock_session.post.call_args
            payload = call_args[1]["json"]
            assert payload["voice_id"] == VOICES["aziza"]

    def test_tts_russian_defaults_to_rachel(self):
        response_data = {"status": "success", "audio_url": "https://x.com/a.mp3"}

        with patch("qanot.voice.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value=response_data)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            asyncio.run(text_to_speech("тест", api_key="k", language="ru"))

            payload = mock_session.post.call_args[1]["json"]
            assert payload["voice_id"] == VOICES["rachel"]

    def test_tts_custom_voice(self):
        response_data = {"status": "success", "audio_url": "https://x.com/a.mp3"}

        with patch("qanot.voice.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value=response_data)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            asyncio.run(text_to_speech("test", api_key="k", voice="sherzod"))

            payload = mock_session.post.call_args[1]["json"]
            assert payload["voice_id"] == VOICES["sherzod"]

    def test_tts_api_error(self):
        with patch("qanot.voice.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_resp = AsyncMock()
            mock_resp.status = 402
            mock_resp.json = AsyncMock(return_value={"error": "Insufficient balance"})
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            with pytest.raises(RuntimeError, match="KotibAI TTS error"):
                asyncio.run(text_to_speech("test", api_key="k"))

    def test_tts_text_truncated_to_5000(self):
        """Text >5000 chars should be truncated."""
        # Long text uses blocking=false, so mock async flow
        post_data = {"status": "processing", "id": "tts_trunc"}
        poll_data = {"status": "completed", "audio_url": "https://x.com/a.mp3"}

        with patch("qanot.voice.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_post_resp = AsyncMock()
            mock_post_resp.status = 200
            mock_post_resp.json = AsyncMock(return_value=post_data)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_post_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            mock_get_resp = AsyncMock()
            mock_get_resp.status = 200
            mock_get_resp.json = AsyncMock(return_value=poll_data)
            mock_session.get = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_get_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            long_text = "a" * 10000
            asyncio.run(text_to_speech(long_text, api_key="k"))

            payload = mock_session.post.call_args[1]["json"]
            assert len(payload["text"]) == 5000


# ── Config integration ────────────────────────────────────


class TestVoiceConfig:
    def test_config_voice_fields(self):
        from qanot.config import Config
        c = Config()
        assert c.kotib_api_key == ""
        assert c.voice_mode == "inbound"
        assert c.voice_name == ""
        assert c.voice_language == ""

    def test_config_load_voice_fields(self):
        import tempfile
        from qanot.config import load_config

        cfg = {
            "bot_token": "test",
            "api_key": "test",
            "kotib_api_key": "kotib-test-key",
            "voice_mode": "always",
            "voice_name": "sherzod",
            "voice_language": "uz",
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(cfg, tmp)
        tmp.close()

        try:
            config = load_config(tmp.name)
            assert config.kotib_api_key == "kotib-test-key"
            assert config.voice_mode == "always"
            assert config.voice_name == "sherzod"
            assert config.voice_language == "uz"
        finally:
            os.unlink(tmp.name)


# ── Edge cases ────────────────────────────────────────────


class TestVoiceEdgeCases:
    def test_transcribe_uzbek_text(self):
        """Verify Uzbek text handling in TranscriptionResult."""
        r = TranscriptionResult(
            text="O'zbekiston Respublikasi — mustaqil davlat",
            language="uz",
        )
        assert "O'zbekiston" in r.text

    def test_transcribe_russian_text(self):
        r = TranscriptionResult(text="Привет, как дела?", language="ru")
        assert "Привет" in r.text

    def test_voices_have_valid_ids(self):
        """All voice IDs should be non-empty strings."""
        for name, voice_id in VOICES.items():
            assert isinstance(voice_id, str)
            assert len(voice_id) > 10, f"Voice {name} has suspiciously short ID"

    def test_tts_long_text_uses_async(self):
        """Text >500 chars should use blocking=false."""
        response_data = {
            "status": "processing",
            "id": "tts_999",
            "message": "TTS generation scheduled.",
        }
        poll_data = {
            "status": "completed",
            "audio_url": "https://x.com/a.mp3",
        }

        with patch("qanot.voice.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            # First call: POST /tts returns processing
            mock_post_resp = AsyncMock()
            mock_post_resp.status = 200
            mock_post_resp.json = AsyncMock(return_value=response_data)
            mock_session.post = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_post_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            # Second call: GET /get-status returns completed
            mock_get_resp = AsyncMock()
            mock_get_resp.status = 200
            mock_get_resp.json = AsyncMock(return_value=poll_data)
            mock_session.get = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_get_resp),
                __aexit__=AsyncMock(return_value=False),
            ))

            long_text = "Salom " * 200  # ~1200 chars
            result = asyncio.run(text_to_speech(long_text, api_key="k"))
            assert result.audio_url == "https://x.com/a.mp3"

            # Verify blocking was set to false
            payload = mock_session.post.call_args[1]["json"]
            assert payload["blocking"] is False
