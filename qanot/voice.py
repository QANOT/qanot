"""Voice processing — KotibAI STT/TTS integration for Telegram voice messages."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

KOTIB_BASE_URL = "https://developer.kotib.ai/api/v1"


@dataclass
class TranscriptionResult:
    """Result from speech-to-text."""

    text: str
    language: str | None = None


@dataclass
class TTSResult:
    """Result from text-to-speech."""

    audio_url: str
    character_count: int = 0


# ── Audio Conversion ─────────────────────────────────────


async def convert_ogg_to_mp3(ogg_path: str) -> str:
    """Convert OGG Opus (Telegram voice) to MP3 via ffmpeg."""
    mp3_path = ogg_path.rsplit(".", 1)[0] + ".mp3"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", ogg_path,
        "-codec:a", "libmp3lame",
        "-q:a", "4",
        mp3_path,
        "-y",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {stderr.decode()[:200]}")
    return mp3_path


async def convert_video_to_mp3(video_path: str) -> str:
    """Extract audio from video note (MP4) to MP3 via ffmpeg."""
    mp3_path = video_path.rsplit(".", 1)[0] + ".mp3"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", video_path,
        "-vn",
        "-codec:a", "libmp3lame",
        "-q:a", "4",
        mp3_path,
        "-y",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg extraction failed: {stderr.decode()[:200]}")
    return mp3_path


# ── KotibAI STT ──────────────────────────────────────────


async def transcribe(
    audio_path: str,
    api_key: str,
    language: str | None = None,
) -> TranscriptionResult:
    """Transcribe audio file using KotibAI STT API.

    Args:
        audio_path: Path to MP3/WAV/M4A audio file.
        api_key: KotibAI API key.
        language: Optional language code (uz/ru/en). Auto-detected if None.

    Returns:
        TranscriptionResult with transcribed text.
    """
    headers = {"Authorization": f"Bearer {api_key}"}

    data = aiohttp.FormData()
    data.add_field(
        "audio",
        open(audio_path, "rb"),
        filename=os.path.basename(audio_path),
    )
    data.add_field("blocking", "true")
    if language:
        data.add_field("language", language)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{KOTIB_BASE_URL}/stt",
            headers=headers,
            data=data,
        ) as resp:
            result = await resp.json()

            if resp.status != 200:
                error = result.get("error", f"HTTP {resp.status}")
                raise RuntimeError(f"KotibAI STT error: {error}")

            if result.get("status") != "success":
                raise RuntimeError(f"KotibAI STT failed: {result}")

            return TranscriptionResult(
                text=result.get("text", ""),
                language=language,
            )


# ── KotibAI TTS ──────────────────────────────────────────

# Available Uzbek voices
VOICES = {
    "aziza": "DAwWafAKHO9QX6qcLggZ",       # Female, Uzbek (default)
    "nargiza": "RNmQxero1wyOi8SD9iMV",      # Female, Uzbek
    "soliha": "XDuXlUPQeAboPqoc7PJM",       # Female, Uzbek
    "sherzod": "wLCDHBSlf2alLqS7YPqK",      # Male, Uzbek
    "rachel": "21m00Tcm4TlvDq8ikWAM",       # Female, Russian/English
    "arnold": "VR6AewLTigWG4xSOukaG",       # Male, Russian/English
}

DEFAULT_VOICE = "aziza"


async def text_to_speech(
    text: str,
    api_key: str,
    language: str = "uz",
    voice: str | None = None,
) -> TTSResult:
    """Convert text to speech using KotibAI TTS API.

    Args:
        text: Text to convert (max 5000 chars).
        api_key: KotibAI API key.
        language: Language code (uz/ru/en).
        voice: Voice name or ID. Defaults based on language.

    Returns:
        TTSResult with audio URL.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Resolve voice ID
    voice_id = None
    if voice:
        voice_id = VOICES.get(voice.lower(), voice)
    if not voice_id:
        # Pick default by language
        if language in ("ru", "en"):
            voice_id = VOICES["rachel"]
        else:
            voice_id = VOICES[DEFAULT_VOICE]

    # Text >500 chars requires blocking=false per API docs
    blocking = len(text) <= 500

    payload = {
        "text": text[:5000],
        "lang": language,
        "voice_id": voice_id,
        "blocking": blocking,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{KOTIB_BASE_URL}/tts",
            headers=headers,
            json=payload,
        ) as resp:
            result = await resp.json()

            if resp.status != 200:
                error = result.get("error", f"HTTP {resp.status}")
                raise RuntimeError(f"KotibAI TTS error: {error}")

            if blocking:
                if result.get("status") != "success":
                    raise RuntimeError(f"KotibAI TTS failed: {result}")
                return TTSResult(
                    audio_url=result.get("audio_url", ""),
                    character_count=result.get("character_count", 0),
                )

            # Async mode — poll for result
            task_id = result.get("id", "")
            if not task_id:
                raise RuntimeError(f"KotibAI TTS: no task_id returned: {result}")

            return await _poll_task(task_id, api_key, session)


async def _poll_task(
    task_id: str,
    api_key: str,
    session: aiohttp.ClientSession,
    max_wait: float = 30.0,
) -> TTSResult:
    """Poll KotibAI get-status endpoint until task completes."""
    headers = {"Authorization": f"Bearer {api_key}"}
    elapsed = 0.0
    interval = 1.0

    while elapsed < max_wait:
        await asyncio.sleep(interval)
        elapsed += interval

        async with session.get(
            f"{KOTIB_BASE_URL}/get-status",
            headers=headers,
            params={"task_id": task_id},
        ) as resp:
            result = await resp.json()
            status = result.get("status", "")

            if status == "completed":
                audio_url = result.get("audio_url", "")
                # For TTS, audio_url might be in result dict
                if not audio_url and isinstance(result.get("result"), dict):
                    audio_url = result["result"].get("audio_url", "")
                return TTSResult(audio_url=audio_url)

            if status in ("failed", "error"):
                raise RuntimeError(f"KotibAI TTS task failed: {result}")

        # Increase interval slightly for longer texts
        interval = min(interval * 1.2, 3.0)

    raise RuntimeError(f"KotibAI TTS task timed out after {max_wait}s")


# ── Download helper ───────────────────────────────────────


async def download_audio(url: str) -> str:
    """Download audio file from URL to a temp file."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to download audio: HTTP {resp.status}")
            data = await resp.read()

    suffix = ".mp3"
    if ".wav" in url:
        suffix = ".wav"
    elif ".ogg" in url:
        suffix = ".ogg"

    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(data)
    tmp.close()
    return tmp.name
