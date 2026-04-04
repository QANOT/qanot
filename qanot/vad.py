"""Voice Activity Detection (VAD) wrapper for real-time speech boundary detection.

Uses Silero VAD (ONNX) for accurate, low-latency speech detection.
Processes 16kHz mono PCM in 512-sample (32ms) chunks.
Returns SPEECH_START/SPEECH_END events for turn boundary detection.
"""

from __future__ import annotations

import enum
import logging
from typing import TYPE_CHECKING

import numpy as np

logger = logging.getLogger(__name__)

# Silero VAD requires exactly 16kHz input
SAMPLE_RATE = 16000
# Chunk size: 512 samples = 32ms at 16kHz (Silero's training chunk size)
CHUNK_SAMPLES = 512
CHUNK_BYTES = CHUNK_SAMPLES * 2  # 16-bit PCM = 1024 bytes per chunk


class VADEvent(enum.Enum):
    """Events emitted by VAD on state transitions."""
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"


class SileroVAD:
    """Silero VAD wrapper for real-time speech boundary detection.

    Processes 16kHz mono PCM16LE in 512-sample chunks.
    Emits SPEECH_START when speech begins after silence,
    and SPEECH_END when silence is detected after speech.

    Uses hysteresis (separate thresholds for start/stop) to prevent toggling.
    """

    def __init__(
        self,
        threshold: float = 0.5,
        min_silence_ms: int = 400,
        min_speech_ms: int = 250,
        speech_pad_ms: int = 30,
    ) -> None:
        self._threshold = threshold
        self._neg_threshold = max(threshold - 0.15, 0.1)  # hysteresis
        self._min_silence_chunks = max(1, min_silence_ms * SAMPLE_RATE // (CHUNK_SAMPLES * 1000))
        self._min_speech_chunks = max(1, min_speech_ms * SAMPLE_RATE // (CHUNK_SAMPLES * 1000))
        self._speech_pad_chunks = max(0, speech_pad_ms * SAMPLE_RATE // (CHUNK_SAMPLES * 1000))

        self._model = None  # Lazy-loaded
        self._is_speech = False
        self._speech_count = 0  # consecutive speech chunks
        self._silence_count = 0  # consecutive silence chunks
        self._triggered = False  # True after SPEECH_START emitted

    def _ensure_model(self) -> None:
        """Lazy-load Silero VAD model on first use (~50ms, ~2MB ONNX)."""
        if self._model is not None:
            return
        try:
            import torch
            model, _ = torch.hub.load(
                "snakers4/silero-vad", "silero_vad",
                trust_repo=True, verbose=False,
            )
            self._model = model
            logger.info("Silero VAD model loaded")
        except ImportError:
            raise RuntimeError(
                "Silero VAD requires torch. Install with: pip install torch"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to load Silero VAD model: {e}")

    def process_chunk(self, pcm_16k_mono: bytes) -> VADEvent | None:
        """Process one 512-sample chunk of 16kHz mono PCM16LE.

        Args:
            pcm_16k_mono: Exactly 1024 bytes (512 samples of 16-bit PCM).

        Returns:
            VADEvent.SPEECH_START — speech detected after silence
            VADEvent.SPEECH_END — silence detected after speech (>min_silence_ms)
            None — no state change
        """
        self._ensure_model()

        import torch

        # Convert PCM bytes to float32 tensor [-1, 1]
        samples = np.frombuffer(pcm_16k_mono[:CHUNK_BYTES], dtype=np.int16)
        if len(samples) < CHUNK_SAMPLES:
            # Pad short chunks with silence
            samples = np.pad(samples, (0, CHUNK_SAMPLES - len(samples)))
        audio_tensor = torch.from_numpy(samples.astype(np.float32) / 32768.0)

        # Get speech probability
        confidence = self._model(audio_tensor, SAMPLE_RATE).item()
        is_speech = confidence >= self._threshold

        if is_speech:
            self._speech_count += 1
            self._silence_count = 0
        else:
            # Use negative threshold (hysteresis) for speech-to-silence transition
            if confidence < self._neg_threshold:
                self._silence_count += 1
                self._speech_count = 0

        # State machine: emit events on transitions
        if not self._triggered:
            # Waiting for speech
            if self._speech_count >= self._min_speech_chunks:
                self._triggered = True
                self._silence_count = 0
                return VADEvent.SPEECH_START
        else:
            # In speech — waiting for silence
            if self._silence_count >= self._min_silence_chunks:
                self._triggered = False
                self._speech_count = 0
                return VADEvent.SPEECH_END

        return None

    def reset(self) -> None:
        """Reset state for a new call or conversation."""
        self._is_speech = False
        self._speech_count = 0
        self._silence_count = 0
        self._triggered = False
        # Reset model state (Silero has internal hidden state)
        if self._model is not None:
            try:
                self._model.reset_states()
            except Exception:
                pass

    @property
    def is_speech_active(self) -> bool:
        """True if VAD is currently detecting speech."""
        return self._triggered
