"""Voice Activity Detection (VAD) wrapper for real-time speech boundary detection.

Uses Silero VAD via the official ``silero-vad`` pip package, which bundles
the ONNX model and runs on ``onnxruntime`` — no torch dependency. This is
the production pattern used by Pipecat, LiveKit, and Modal voice pipelines
(torch.hub's silero flavour needs a 600MB+ install and is ~2x slower).

Processes 16kHz mono PCM in 512-sample (32ms) chunks.
Returns SPEECH_START/SPEECH_END events for turn boundary detection.
"""

from __future__ import annotations

import enum
import logging

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
        """Lazy-load Silero VAD (ONNX) on first use.

        The ``silero-vad`` pip package exposes ``load_silero_vad(onnx=True)``
        which returns an inference callable that takes a (float32 numpy
        array, sample_rate) and returns a speech probability tensor. No
        torch needed — onnxruntime handles everything.
        """
        if self._model is not None:
            return
        try:
            from silero_vad import load_silero_vad
        except ImportError as e:
            raise RuntimeError(
                "silero-vad package not installed. Add silero-vad>=5.1 to "
                "requirements.txt and rebuild the bot image."
            ) from e
        try:
            self._model = load_silero_vad(onnx=True)
            logger.info("Silero VAD loaded (ONNX backend)")
        except Exception as e:
            raise RuntimeError(f"Failed to load Silero VAD model: {e}") from e

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

        # Convert PCM bytes to float32 array [-1, 1]. silero-vad's ONNX
        # model accepts either a torch tensor OR a numpy float32 array;
        # we use numpy to avoid pulling torch in.
        samples = np.frombuffer(pcm_16k_mono[:CHUNK_BYTES], dtype=np.int16)
        if len(samples) < CHUNK_SAMPLES:
            samples = np.pad(samples, (0, CHUNK_SAMPLES - len(samples)))
        audio = samples.astype(np.float32) / 32768.0

        # Run inference. The package internally wraps onnxruntime and
        # returns either a torch tensor (if available) or a numpy array
        # with .item() / float-coercible shape.
        try:
            result = self._model(audio, SAMPLE_RATE)
        except Exception as e:
            logger.error("VAD model inference failed: %s", e)
            return None
        # Normalise to a float. silero returns torch.Tensor when torch is
        # installed, else a numpy array — both expose .item().
        if hasattr(result, "item"):
            confidence = float(result.item())
        else:
            confidence = float(result)
        is_speech = confidence >= self._threshold

        # Diagnostic: log every 30th chunk (~1s) with confidence so we
        # can see whether VAD is seeing speech-like probabilities.
        self._chunk_count = getattr(self, "_chunk_count", 0) + 1
        if self._chunk_count % 30 == 0:
            logger.info(
                "VAD chunk %d: conf=%.3f (thr=%.2f) is_speech=%s "
                "speech_count=%d silence_count=%d triggered=%s",
                self._chunk_count, confidence, self._threshold,
                is_speech, self._speech_count, self._silence_count,
                self._triggered,
            )

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
        self._chunk_count = 0
        # Reset model state (Silero has internal hidden state carried
        # across chunks — must clear between conversations to avoid
        # bleed).
        if self._model is not None:
            for method in ("reset_states", "reset"):
                fn = getattr(self._model, method, None)
                if callable(fn):
                    try:
                        fn()
                        break
                    except Exception:
                        pass

    @property
    def is_speech_active(self) -> bool:
        """True if VAD is currently detecting speech."""
        return self._triggered
