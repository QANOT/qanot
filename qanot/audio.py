"""Real-time PCM audio utilities for voice call pipeline.

Handles resampling between Telegram voice chat format (48kHz stereo)
and STT/TTS format (16kHz mono) using numpy for vectorized speed.
All functions operate on raw PCM16LE bytes — no file I/O in the hot path.
"""

from __future__ import annotations

import io
import logging
import struct
import tempfile
import wave
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Telegram voice chat audio format
VC_SAMPLE_RATE = 48000
VC_CHANNELS = 2  # stereo
VC_SAMPLE_WIDTH = 2  # 16-bit = 2 bytes
VC_FRAME_DURATION_MS = 20
VC_FRAME_BYTES = VC_SAMPLE_RATE * VC_CHANNELS * VC_SAMPLE_WIDTH * VC_FRAME_DURATION_MS // 1000  # 3840

# STT/VAD processing format
STT_SAMPLE_RATE = 16000
STT_CHANNELS = 1  # mono
STT_SAMPLE_WIDTH = 2

# Resampling ratio
_DOWNSAMPLE_FACTOR = VC_SAMPLE_RATE // STT_SAMPLE_RATE  # 3


def resample_48k_stereo_to_16k_mono(pcm: bytes) -> bytes:
    """Downsample 48kHz stereo PCM16LE to 16kHz mono PCM16LE.

    Fast path: stereo→mono via channel average, then decimate by 3.
    Skips anti-aliasing filter since WebRTC already bandlimits.
    ~0.05ms per 20ms frame on modern hardware.
    """
    if not pcm:
        return b""
    samples = np.frombuffer(pcm, dtype=np.int16)
    if len(samples) < 2:
        return b""
    # Stereo to mono: average L+R channels
    left = samples[0::2].astype(np.int32)
    right = samples[1::2].astype(np.int32)
    mono = ((left + right) // 2).astype(np.int16)
    # Decimate 48kHz → 16kHz (take every 3rd sample)
    decimated = mono[::_DOWNSAMPLE_FACTOR]
    return decimated.tobytes()


def resample_16k_mono_to_48k_stereo(pcm: bytes) -> bytes:
    """Upsample 16kHz mono PCM16LE to 48kHz stereo PCM16LE.

    Uses sample repetition (not interpolation) for speed.
    Acceptable quality for voice — artifacts are above 8kHz (inaudible in speech).
    """
    if not pcm:
        return b""
    samples = np.frombuffer(pcm, dtype=np.int16)
    if len(samples) == 0:
        return b""
    # Upsample by 3 (repeat each sample)
    upsampled = np.repeat(samples, _DOWNSAMPLE_FACTOR)
    # Mono to stereo (duplicate channel)
    stereo = np.empty(len(upsampled) * 2, dtype=np.int16)
    stereo[0::2] = upsampled
    stereo[1::2] = upsampled
    return stereo.tobytes()


def pcm_to_wav_bytes(pcm_16k_mono: bytes) -> bytes:
    """Wrap raw 16kHz mono PCM in a WAV header. Returns complete WAV bytes.

    Used for STT providers that require file-like input (e.g., Muxlisa, Whisper).
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(STT_CHANNELS)
        wf.setsampwidth(STT_SAMPLE_WIDTH)
        wf.setframerate(STT_SAMPLE_RATE)
        wf.writeframes(pcm_16k_mono)
    return buf.getvalue()


def pcm_to_wav_file(pcm_16k_mono: bytes) -> str:
    """Write 16kHz mono PCM to a temporary WAV file. Returns path.

    Caller is responsible for cleanup (os.unlink).
    """
    wav_bytes = pcm_to_wav_bytes(pcm_16k_mono)
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="qanot_vc_")
    try:
        with open(fd, "wb") as f:
            f.write(wav_bytes)
    except Exception:
        import os
        os.close(fd)
        raise
    return path


def wav_bytes_to_pcm(wav_data: bytes) -> tuple[bytes, int, int]:
    """Extract raw PCM from WAV bytes.

    Returns (pcm_bytes, sample_rate, channels).
    """
    buf = io.BytesIO(wav_data)
    with wave.open(buf, "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        return pcm, wf.getframerate(), wf.getnchannels()


async def tts_result_to_vc_pcm(tts_result, config) -> bytes | None:
    """Convert a TTSResult from voice.text_to_speech() to 48kHz stereo PCM.

    Handles all provider output formats:
    - audio_data (bytes): WAV or MP3 — decode and resample
    - audio_url (str): Download, decode, resample

    Returns 48kHz stereo PCM bytes ready for send_frame(), or None on error.
    """
    try:
        raw_pcm_16k: bytes | None = None

        if tts_result.audio_data:
            # WAV bytes (Muxlisa) or MP3 bytes (Aisha)
            raw_pcm_16k = _decode_audio_bytes(tts_result.audio_data)
        elif tts_result.audio_url:
            # Download and decode (KotibAI, Aisha URL mode)
            from qanot.voice import download_audio
            audio_path = await download_audio(tts_result.audio_url)
            if audio_path:
                try:
                    raw_pcm_16k = await _decode_audio_file(audio_path)
                finally:
                    import os
                    try:
                        os.unlink(audio_path)
                    except OSError:
                        pass

        if raw_pcm_16k is None:
            return None

        return resample_16k_mono_to_48k_stereo(raw_pcm_16k)

    except Exception as e:
        logger.error("Failed to convert TTS to VC PCM: %s", e)
        return None


def _decode_audio_bytes(data: bytes) -> bytes | None:
    """Decode WAV or MP3 bytes to 16kHz mono PCM."""
    # Try WAV first (Muxlisa returns WAV)
    if data[:4] == b"RIFF":
        pcm, sr, ch = wav_bytes_to_pcm(data)
        return _ensure_16k_mono(pcm, sr, ch)

    # Assume MP3/other — need ffmpeg
    return _ffmpeg_decode_bytes(data)


async def _decode_audio_file(path: str) -> bytes | None:
    """Decode an audio file to 16kHz mono PCM via ffmpeg."""
    import asyncio
    import subprocess

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg", "-i", path,
                "-f", "s16le", "-ar", "16000", "-ac", "1",
                "pipe:1",
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception as e:
        logger.warning("ffmpeg decode failed for %s: %s", path, e)
    return None


def _ffmpeg_decode_bytes(data: bytes) -> bytes | None:
    """Decode audio bytes (MP3/OGG/etc) to 16kHz mono PCM via ffmpeg pipe."""
    import subprocess

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-i", "pipe:0",
                "-f", "s16le", "-ar", "16000", "-ac", "1",
                "pipe:1",
            ],
            input=data,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception as e:
        logger.warning("ffmpeg pipe decode failed: %s", e)
    return None


def _ensure_16k_mono(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    """Resample arbitrary PCM to 16kHz mono."""
    samples = np.frombuffer(pcm, dtype=np.int16)

    # Stereo to mono
    if channels == 2:
        left = samples[0::2].astype(np.int32)
        right = samples[1::2].astype(np.int32)
        samples = ((left + right) // 2).astype(np.int16)

    # Resample if needed
    if sample_rate != STT_SAMPLE_RATE:
        factor = sample_rate / STT_SAMPLE_RATE
        new_len = int(len(samples) / factor)
        indices = np.linspace(0, len(samples) - 1, new_len).astype(int)
        samples = samples[indices]

    return samples.tobytes()


def split_pcm_frames(pcm: bytes, frame_bytes: int = VC_FRAME_BYTES) -> list[bytes]:
    """Split PCM buffer into fixed-size frames for send_frame().

    Last frame is zero-padded if shorter than frame_bytes.
    """
    frames = []
    for i in range(0, len(pcm), frame_bytes):
        chunk = pcm[i:i + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        frames.append(chunk)
    return frames
