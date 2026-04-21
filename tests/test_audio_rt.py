"""Tests for real-time audio utilities (qanot/audio.py)."""

import numpy as np
import pytest

from qanot.audio import (
    VC_FRAME_BYTES,
    pcm_to_wav_bytes,
    pcm_to_wav_file,
    resample_16k_mono_to_48k_stereo,
    resample_48k_stereo_to_16k_mono,
    split_pcm_frames,
    wav_bytes_to_pcm,
    _ensure_16k_mono,
)


class TestResample48kTo16k:
    """Test downsampling from VC format to STT format."""

    def test_basic_downsample(self):
        # 20ms frame at 48kHz stereo: 960 samples * 2 channels * 2 bytes = 3840
        pcm_48k = np.zeros(960 * 2, dtype=np.int16).tobytes()
        result = resample_48k_stereo_to_16k_mono(pcm_48k)
        # Expected: 960/3 = 320 samples * 2 bytes = 640 bytes
        assert len(result) == 640

    def test_preserves_signal(self):
        # Create a 48kHz stereo sine wave
        t = np.arange(960 * 2)  # 960 stereo samples
        samples = (np.sin(t * 0.1) * 10000).astype(np.int16)
        result = resample_48k_stereo_to_16k_mono(samples.tobytes())
        assert len(result) > 0
        # Verify output is valid PCM
        output = np.frombuffer(result, dtype=np.int16)
        assert output.dtype == np.int16

    def test_empty_input(self):
        assert resample_48k_stereo_to_16k_mono(b"") == b""

    def test_frame_size(self):
        # Standard VC frame (10ms, 1920 bytes at 48kHz stereo 16-bit):
        # → 480 stereo samples → 480 mono → 160 samples @ 16kHz → 320 bytes
        pcm = b"\x00" * VC_FRAME_BYTES
        result = resample_48k_stereo_to_16k_mono(pcm)
        samples_48k_stereo = VC_FRAME_BYTES // 4
        expected_bytes = (samples_48k_stereo // 3) * 2
        assert len(result) == expected_bytes


class TestResample16kTo48k:
    """Test upsampling from TTS format to VC format."""

    def test_basic_upsample(self):
        # 320 mono samples at 16kHz
        pcm_16k = np.zeros(320, dtype=np.int16).tobytes()
        result = resample_16k_mono_to_48k_stereo(pcm_16k)
        # Expected: 320 * 3 * 2 channels * 2 bytes = 3840
        assert len(result) == 3840

    def test_roundtrip_preserves_shape(self):
        # Downsample then upsample should preserve sample count
        original = np.zeros(960 * 2, dtype=np.int16).tobytes()
        down = resample_48k_stereo_to_16k_mono(original)
        up = resample_16k_mono_to_48k_stereo(down)
        assert len(up) == len(original)

    def test_empty_input(self):
        assert resample_16k_mono_to_48k_stereo(b"") == b""


class TestWavConversion:
    """Test PCM ↔ WAV conversion."""

    def test_pcm_to_wav_bytes(self):
        pcm = np.zeros(16000, dtype=np.int16).tobytes()  # 1 second
        wav = pcm_to_wav_bytes(pcm)
        assert wav[:4] == b"RIFF"
        assert b"WAVE" in wav[:12]

    def test_wav_roundtrip(self):
        original_pcm = np.arange(1000, dtype=np.int16).tobytes()
        wav = pcm_to_wav_bytes(original_pcm)
        pcm, sr, ch = wav_bytes_to_pcm(wav)
        assert sr == 16000
        assert ch == 1
        assert pcm == original_pcm

    def test_pcm_to_wav_file(self):
        import os
        pcm = np.zeros(1000, dtype=np.int16).tobytes()
        path = pcm_to_wav_file(pcm)
        try:
            assert os.path.exists(path)
            assert path.endswith(".wav")
            with open(path, "rb") as f:
                assert f.read(4) == b"RIFF"
        finally:
            os.unlink(path)


class TestSplitFrames:
    """Test PCM frame splitting for send_frame()."""

    def test_exact_multiple(self):
        pcm = b"\x01" * (VC_FRAME_BYTES * 3)
        frames = split_pcm_frames(pcm)
        assert len(frames) == 3
        assert all(len(f) == VC_FRAME_BYTES for f in frames)

    def test_short_last_frame_padded(self):
        pcm = b"\x01" * (VC_FRAME_BYTES + 100)
        frames = split_pcm_frames(pcm)
        assert len(frames) == 2
        assert len(frames[0]) == VC_FRAME_BYTES
        assert len(frames[1]) == VC_FRAME_BYTES  # padded
        # Verify padding is zeros
        assert frames[1][100:] == b"\x00" * (VC_FRAME_BYTES - 100)

    def test_empty_input(self):
        assert split_pcm_frames(b"") == []


class TestEnsure16kMono:
    """Test arbitrary PCM → 16kHz mono conversion."""

    def test_already_16k_mono(self):
        pcm = np.arange(100, dtype=np.int16).tobytes()
        result = _ensure_16k_mono(pcm, 16000, 1)
        assert result == pcm

    def test_stereo_to_mono(self):
        # 100 stereo samples → 100 mono samples
        pcm = np.zeros(200, dtype=np.int16).tobytes()
        result = _ensure_16k_mono(pcm, 16000, 2)
        assert len(result) == 200  # 100 samples * 2 bytes

    def test_48k_to_16k(self):
        # 4800 mono samples at 48kHz → ~1600 at 16kHz
        pcm = np.zeros(4800, dtype=np.int16).tobytes()
        result = _ensure_16k_mono(pcm, 48000, 1)
        output = np.frombuffer(result, dtype=np.int16)
        assert len(output) == 1600
