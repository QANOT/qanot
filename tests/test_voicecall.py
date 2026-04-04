"""Tests for voice call module (qanot/voicecall.py)."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qanot.voicecall import (
    AudioPipeline,
    CallSession,
    MIN_SPEECH_BYTES,
    VoiceCallManager,
)


class TestCallSession:
    """Test CallSession state management."""

    def test_creation(self):
        session = CallSession(chat_id=-100123, user_id=111, conv_key="vc_-100123")
        assert session.chat_id == -100123
        assert session.user_id == 111
        assert session.conv_key == "vc_-100123"
        assert session.is_speaking is False
        assert session._llm_task is None

    def test_timestamps(self):
        session = CallSession(chat_id=1, user_id=1, conv_key="vc_1")
        assert session.started_at > 0
        assert session.last_speech_at > 0


class TestAudioPipelineCancelPlayback:
    """Test barge-in playback cancellation."""

    def test_cancel_clears_queue(self):
        # Create minimal pipeline with mock dependencies
        pipeline = AudioPipeline.__new__(AudioPipeline)
        pipeline._outbound_queue = asyncio.Queue(maxsize=100)
        pipeline._session = CallSession(chat_id=1, user_id=1, conv_key="vc_1")

        # Fill queue
        for _ in range(10):
            pipeline._outbound_queue.put_nowait(b"\x00" * 100)
        assert pipeline._outbound_queue.qsize() == 10

        # Cancel
        pipeline.cancel_playback()
        assert pipeline._outbound_queue.empty()
        assert pipeline._session.is_speaking is False

    def test_cancel_empty_queue_no_error(self):
        pipeline = AudioPipeline.__new__(AudioPipeline)
        pipeline._outbound_queue = asyncio.Queue()
        pipeline._session = CallSession(chat_id=1, user_id=1, conv_key="vc_1")
        pipeline.cancel_playback()  # Should not raise


class TestVoiceCallManagerConfig:
    """Test VoiceCallManager configuration validation."""

    def test_disabled_by_default(self):
        config = MagicMock()
        config.voicecall_enabled = False
        config.voicecall_vad_threshold = 0.5
        config.voicecall_silence_ms = 400
        config.voicecall_min_speech_ms = 250
        # Manager can be created even when disabled
        agent = MagicMock()
        manager = VoiceCallManager(config=config, agent=agent)
        assert not manager._started

    def test_is_in_call_empty(self):
        config = MagicMock()
        config.voicecall_vad_threshold = 0.5
        config.voicecall_silence_ms = 400
        config.voicecall_min_speech_ms = 250
        agent = MagicMock()
        manager = VoiceCallManager(config=config, agent=agent)
        assert not manager.is_in_call(-100123)

    def test_max_calls_tracking(self):
        config = MagicMock()
        config.voicecall_vad_threshold = 0.5
        config.voicecall_silence_ms = 400
        config.voicecall_min_speech_ms = 250
        config.voicecall_max_calls = 2
        agent = MagicMock()
        manager = VoiceCallManager(config=config, agent=agent)

        # Manually add sessions
        manager._active_calls[-100] = CallSession(chat_id=-100, user_id=1, conv_key="vc_-100")
        manager._active_calls[-200] = CallSession(chat_id=-200, user_id=2, conv_key="vc_-200")

        assert manager.is_in_call(-100)
        assert manager.is_in_call(-200)
        assert not manager.is_in_call(-300)


class TestMinSpeechBytes:
    """Test minimum speech duration constant."""

    def test_min_speech_bytes_250ms(self):
        # 250ms at 16kHz mono 16-bit = 16000 * 2 * 0.25 = 8000
        assert MIN_SPEECH_BYTES == 8000

    def test_min_speech_filters_short_noise(self):
        # 100ms of audio = 3200 bytes < 8000 (should be filtered)
        short_noise = b"\x00" * 3200
        assert len(short_noise) < MIN_SPEECH_BYTES

    def test_min_speech_accepts_real_speech(self):
        # 500ms of audio = 16000 bytes > 8000 (should be accepted)
        real_speech = b"\x00" * 16000
        assert len(real_speech) >= MIN_SPEECH_BYTES
