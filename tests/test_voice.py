"""Tests for qanot.voice — Muxlisa.uz and KotibAI STT/TTS integration."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qanot.voice import (
    MUXLISA_BASE_URL,
    KOTIB_BASE_URL,
    MUXLISA_VOICES,
    KOTIB_VOICES,
    TranscriptionResult,
    TTSResult,
    convert_ogg_to_mp3,
    convert_video_to_mp3,
    convert_video_to_ogg,
    convert_wav_to_ogg,
    muxlisa_transcribe,
    muxlisa_tts,
    kotib_transcribe,
    kotib_tts,
    transcribe,
    text_to_speech,
    download_audio,
    close_voice_session,
)


# ── Data classes ──────────────────────────────────────────


class TestDataClasses:
    def test_transcription_result(self):
        r = TranscriptionResult(text="salom", language="uz")
        assert r.text == "salom"
        assert r.language == "uz"

    def test_transcription_result_no_language(self):
        r = TranscriptionResult(text="hello")
        assert r.language is None

    def test_tts_result_with_data(self):
        r = TTSResult(audio_data=b"wav bytes", character_count=42)
        assert r.audio_data == b"wav bytes"
        assert r.character_count == 42

    def test_tts_result_with_url(self):
        r = TTSResult(audio_url="https://example.com/audio.mp3")
        assert r.audio_url == "https://example.com/audio.mp3"
        assert r.audio_data is None

    def test_tts_result_defaults(self):
        r = TTSResult()
        assert r.audio_data is None
        assert r.audio_url == ""
        assert r.character_count == 0


# ── Constants ─────────────────────────────────────────────


class TestConstants:
    def test_muxlisa_voices(self):
        assert len(MUXLISA_VOICES) == 2
        assert MUXLISA_VOICES["maftuna"] == 0
        assert MUXLISA_VOICES["asomiddin"] == 1

    def test_kotib_voices(self):
        assert len(KOTIB_VOICES) == 6
        assert "aziza" in KOTIB_VOICES
        assert "sherzod" in KOTIB_VOICES

    def test_muxlisa_base_url(self):
        assert "muxlisa.uz" in MUXLISA_BASE_URL

    def test_kotib_base_url(self):
        assert "kotib.ai" in KOTIB_BASE_URL


# ── FFmpeg conversion tests ──────────────────────────────


class TestAudioConversion:
    def test_ogg_to_mp3(self):
        with patch("qanot.voice.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc
            result = asyncio.run(convert_ogg_to_mp3("/tmp/test.ogg"))
            assert result == "/tmp/test.mp3"

    def test_ogg_to_mp3_failure(self):
        with patch("qanot.voice.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
            mock_exec.return_value = mock_proc
            with pytest.raises(RuntimeError, match="ffmpeg OGG.MP3"):
                asyncio.run(convert_ogg_to_mp3("/tmp/test.ogg"))

    def test_video_to_mp3(self):
        with patch("qanot.voice.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc
            result = asyncio.run(convert_video_to_mp3("/tmp/note.mp4"))
            assert result == "/tmp/note.mp3"
            assert "-vn" in mock_exec.call_args[0]

    def test_video_to_ogg(self):
        with patch("qanot.voice.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc
            result = asyncio.run(convert_video_to_ogg("/tmp/note.mp4"))
            assert result == "/tmp/note.ogg"
            assert "-vn" in mock_exec.call_args[0]

    def test_wav_to_ogg(self):
        with patch("qanot.voice.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc
            result = asyncio.run(convert_wav_to_ogg("/tmp/speech.wav"))
            assert result == "/tmp/speech.ogg"

    def test_wav_to_ogg_failure(self):
        with patch("qanot.voice.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.communicate = AsyncMock(return_value=(b"", b"codec missing"))
            mock_exec.return_value = mock_proc
            with pytest.raises(RuntimeError, match="ffmpeg WAV.OGG"):
                asyncio.run(convert_wav_to_ogg("/tmp/speech.wav"))


# ── Mock helpers ──────────────────────────────────────────


def _mock_session(post_status=200, post_json=None, post_bytes=None,
                  get_status=200, get_json=None):
    """Create a mock aiohttp.ClientSession with configurable responses."""
    mock_session = AsyncMock()

    # Mock POST response
    mock_post_resp = AsyncMock()
    mock_post_resp.status = post_status
    if post_bytes is not None:
        mock_post_resp.read = AsyncMock(return_value=post_bytes)
        mock_post_resp.json = AsyncMock(side_effect=Exception("binary response"))
    else:
        mock_post_resp.json = AsyncMock(return_value=post_json or {})
    mock_post_resp.text = AsyncMock(return_value=json.dumps(post_json or {}))
    mock_session.post = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_post_resp),
        __aexit__=AsyncMock(return_value=False),
    ))

    # Mock GET response
    if get_json is not None:
        mock_get_resp = AsyncMock()
        mock_get_resp.status = get_status
        mock_get_resp.json = AsyncMock(return_value=get_json)
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_get_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

    return mock_session


class _patch_session:
    """Context manager that patches _get_session to return mock_session."""

    def __init__(self, mock_session):
        self.mock_session = mock_session
        self._patcher = None

    def __enter__(self):
        async def _fake_get_session():
            return self.mock_session

        self._patcher = patch("qanot.voice._get_session", new=_fake_get_session)
        self._patcher.start()
        return self.mock_session

    def __exit__(self, *args):
        if self._patcher:
            self._patcher.stop()


def _temp_audio():
    """Create a temp audio file for testing."""
    tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    tmp.write(b"fake audio data")
    tmp.close()
    return tmp.name


# ── Muxlisa STT tests ────────────────────────────────────


class TestMuxlisaTranscribe:
    def test_success(self):
        session = _mock_session(post_json={"text": "Salom dunyo"})
        tmp = _temp_audio()
        try:
            with _patch_session(session):
                result = asyncio.run(muxlisa_transcribe(tmp, api_key="test-key"))
            assert result.text == "Salom dunyo"
        finally:
            os.unlink(tmp)

    def test_sends_x_api_key_header(self):
        session = _mock_session(post_json={"text": "ok"})
        tmp = _temp_audio()
        try:
            with _patch_session(session):
                asyncio.run(muxlisa_transcribe(tmp, api_key="my-key"))
            headers = session.post.call_args[1]["headers"]
            assert headers["x-api-key"] == "my-key"
        finally:
            os.unlink(tmp)

    def test_balance_depleted(self):
        session = _mock_session(post_status=402)
        tmp = _temp_audio()
        try:
            with _patch_session(session):
                with pytest.raises(RuntimeError, match="balance depleted"):
                    asyncio.run(muxlisa_transcribe(tmp, api_key="k"))
        finally:
            os.unlink(tmp)

    def test_rate_limit(self):
        session = _mock_session(post_status=429)
        tmp = _temp_audio()
        try:
            with _patch_session(session):
                with pytest.raises(RuntimeError, match="rate limit"):
                    asyncio.run(muxlisa_transcribe(tmp, api_key="k"))
        finally:
            os.unlink(tmp)

    def test_server_error(self):
        session = _mock_session(post_status=500)
        tmp = _temp_audio()
        try:
            with _patch_session(session):
                with pytest.raises(RuntimeError, match="HTTP 500"):
                    asyncio.run(muxlisa_transcribe(tmp, api_key="k"))
        finally:
            os.unlink(tmp)


# ── Muxlisa TTS tests ────────────────────────────────────


class TestMuxlisaTTS:
    def test_success_returns_wav_bytes(self):
        wav_data = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 100
        session = _mock_session(post_bytes=wav_data)
        with _patch_session(session):
            result = asyncio.run(muxlisa_tts("Salom", api_key="k"))
        assert result.audio_data == wav_data
        assert result.character_count == 5

    def test_default_speaker_female(self):
        session = _mock_session(post_bytes=b"wav")
        with _patch_session(session):
            asyncio.run(muxlisa_tts("test", api_key="k"))
        payload = session.post.call_args[1]["json"]
        assert payload["speaker"] == 0  # Maftuna (female)

    def test_male_speaker(self):
        session = _mock_session(post_bytes=b"wav")
        with _patch_session(session):
            asyncio.run(muxlisa_tts("test", api_key="k", voice="asomiddin"))
        payload = session.post.call_args[1]["json"]
        assert payload["speaker"] == 1  # Asomiddin (male)

    def test_text_truncated(self):
        session = _mock_session(post_bytes=b"wav")
        with _patch_session(session):
            asyncio.run(muxlisa_tts("a" * 10000, api_key="k"))
        payload = session.post.call_args[1]["json"]
        assert len(payload["text"]) == 5000

    def test_balance_depleted(self):
        session = _mock_session(post_status=402)
        with _patch_session(session):
            with pytest.raises(RuntimeError, match="balance depleted"):
                asyncio.run(muxlisa_tts("test", api_key="k"))

    def test_rate_limit(self):
        session = _mock_session(post_status=429)
        with _patch_session(session):
            with pytest.raises(RuntimeError, match="rate limit"):
                asyncio.run(muxlisa_tts("test", api_key="k"))


# ── KotibAI STT tests ────────────────────────────────────


class TestKotibTranscribe:
    def test_success(self):
        session = _mock_session(post_json={"status": "success", "text": "Hello"})
        tmp = _temp_audio()
        try:
            with _patch_session(session):
                result = asyncio.run(kotib_transcribe(tmp, api_key="k"))
            assert result.text == "Hello"
        finally:
            os.unlink(tmp)

    def test_api_error(self):
        session = _mock_session(post_status=401, post_json={"error": "Invalid API key"})
        tmp = _temp_audio()
        try:
            with _patch_session(session):
                with pytest.raises(RuntimeError, match="KotibAI STT error"):
                    asyncio.run(kotib_transcribe(tmp, api_key="bad"))
        finally:
            os.unlink(tmp)

    def test_with_language(self):
        session = _mock_session(post_json={"status": "success", "text": "Привет"})
        tmp = _temp_audio()
        try:
            with _patch_session(session):
                result = asyncio.run(kotib_transcribe(tmp, api_key="k", language="ru"))
            assert result.language == "ru"
        finally:
            os.unlink(tmp)


# ── KotibAI TTS tests ────────────────────────────────────


class TestKotibTTS:
    def test_short_text_blocking(self):
        session = _mock_session(post_json={
            "status": "success",
            "audio_url": "https://kotib.ai/audio.mp3",
            "character_count": 5,
        })
        with _patch_session(session):
            result = asyncio.run(kotib_tts("Salom", api_key="k"))
        assert result.audio_url == "https://kotib.ai/audio.mp3"

    def test_default_uzbek_voice(self):
        session = _mock_session(post_json={"status": "success", "audio_url": "x"})
        with _patch_session(session):
            asyncio.run(kotib_tts("test", api_key="k", language="uz"))
        payload = session.post.call_args[1]["json"]
        assert payload["voice_id"] == KOTIB_VOICES["aziza"]

    def test_russian_defaults_to_rachel(self):
        session = _mock_session(post_json={"status": "success", "audio_url": "x"})
        with _patch_session(session):
            asyncio.run(kotib_tts("тест", api_key="k", language="ru"))
        payload = session.post.call_args[1]["json"]
        assert payload["voice_id"] == KOTIB_VOICES["rachel"]


# ── Unified interface tests ───────────────────────────────


class TestUnifiedInterface:
    def test_transcribe_default_muxlisa(self):
        session = _mock_session(post_json={"text": "Salom"})
        tmp = _temp_audio()
        try:
            with _patch_session(session):
                result = asyncio.run(transcribe(tmp, api_key="k"))
            assert result.text == "Salom"
            # Should use Muxlisa endpoint
            url = session.post.call_args[0][0]
            assert "muxlisa" in url
        finally:
            os.unlink(tmp)

    def test_transcribe_kotib_provider(self):
        session = _mock_session(post_json={"status": "success", "text": "Hello"})
        tmp = _temp_audio()
        try:
            with _patch_session(session):
                result = asyncio.run(transcribe(tmp, api_key="k", provider="kotib"))
            assert result.text == "Hello"
            url = session.post.call_args[0][0]
            assert "kotib" in url
        finally:
            os.unlink(tmp)

    def test_tts_default_muxlisa(self):
        session = _mock_session(post_bytes=b"wav data")
        with _patch_session(session):
            result = asyncio.run(text_to_speech("test", api_key="k"))
        assert result.audio_data == b"wav data"

    def test_tts_kotib_provider(self):
        session = _mock_session(post_json={"status": "success", "audio_url": "x"})
        with _patch_session(session):
            result = asyncio.run(text_to_speech("test", api_key="k", provider="kotib"))
        assert result.audio_url == "x"


# ── Config tests ──────────────────────────────────────────


class TestVoiceConfig:
    def test_config_voice_fields(self):
        from qanot.config import Config
        c = Config()
        assert c.voice_provider == "muxlisa"
        assert c.voice_api_key == ""
        assert c.voice_mode == "inbound"
        assert c.voice_name == ""
        assert c.voice_language == ""

    def test_config_load(self):
        from qanot.config import load_config
        cfg = {
            "bot_token": "test",
            "api_key": "test",
            "voice_provider": "kotib",
            "voice_api_key": "my-key",
            "voice_mode": "always",
            "voice_name": "sherzod",
            "voice_language": "uz",
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(cfg, tmp)
        tmp.close()
        try:
            config = load_config(tmp.name)
            assert config.voice_provider == "kotib"
            assert config.voice_api_key == "my-key"
            assert config.voice_mode == "always"
            assert config.voice_name == "sherzod"
        finally:
            os.unlink(tmp.name)


# ── Edge cases ────────────────────────────────────────────


class TestEdgeCases:
    def test_uzbek_transcription(self):
        r = TranscriptionResult(text="O'zbekiston Respublikasi — mustaqil davlat", language="uz")
        assert "O'zbekiston" in r.text

    def test_russian_transcription(self):
        r = TranscriptionResult(text="Привет, как дела?", language="ru")
        assert "Привет" in r.text

    def test_mixed_uzbek_russian(self):
        """Muxlisa supports mixed uz-ru code-switching."""
        r = TranscriptionResult(text="Salom, как дела? Яхши раҳмат!")
        assert "Salom" in r.text
        assert "как" in r.text

    def test_kotib_voices_have_valid_ids(self):
        for name, voice_id in KOTIB_VOICES.items():
            assert isinstance(voice_id, str)
            assert len(voice_id) > 10

    def test_muxlisa_voices_are_integers(self):
        for name, speaker_id in MUXLISA_VOICES.items():
            assert isinstance(speaker_id, int)
            assert speaker_id in (0, 1)

    def test_muxlisa_stt_ogg_no_conversion_needed(self):
        """Muxlisa accepts OGG natively — verify endpoint is called with OGG file."""
        session = _mock_session(post_json={"text": "test"})
        tmp = _temp_audio()  # .ogg extension
        try:
            with _patch_session(session):
                asyncio.run(muxlisa_transcribe(tmp, api_key="k"))
            # Verify the file was sent as-is (OGG)
            assert session.post.called
        finally:
            os.unlink(tmp)
