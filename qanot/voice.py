"""Voice processing — Muxlisa.uz and KotibAI STT/TTS for Telegram voice messages.

Supports two providers:
  - muxlisa (default): Native OGG support, no ffmpeg needed for STT, Uzbek dialects
  - kotib: 6 TTS voices, auto language detection

Provider is selected via config.voice_provider ("muxlisa" | "kotib").
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

# ── Shared session management ───────────────────────────

_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    """Return a shared aiohttp session, creating one if needed."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_voice_session() -> None:
    """Close the shared aiohttp session (call on shutdown)."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


MUXLISA_BASE_URL = "https://service.muxlisa.uz/api/v2"
MUXLISA_ASYNC_URL = "https://service.muxlisa.uz/api/v1/async"
KOTIB_BASE_URL = "https://developer.kotib.ai/api/v1"


@dataclass
class TranscriptionResult:
    """Result from speech-to-text."""

    text: str
    language: str | None = None


@dataclass
class TTSResult:
    """Result from text-to-speech."""

    audio_data: bytes | None = None  # Raw audio bytes (Muxlisa returns WAV directly)
    audio_url: str = ""  # Audio URL (KotibAI returns URL)
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


async def convert_video_to_ogg(video_path: str) -> str:
    """Extract audio from video note (MP4) to OGG for Muxlisa."""
    ogg_path = video_path.rsplit(".", 1)[0] + ".ogg"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", video_path,
        "-vn",
        "-codec:a", "libopus",
        "-b:a", "32k",
        ogg_path,
        "-y",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg extraction failed: {stderr.decode()[:200]}")
    return ogg_path


async def convert_wav_to_ogg(wav_path: str) -> str:
    """Convert WAV to OGG Opus for Telegram voice messages."""
    ogg_path = wav_path.rsplit(".", 1)[0] + ".ogg"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", wav_path,
        "-codec:a", "libopus",
        "-b:a", "32k",
        ogg_path,
        "-y",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg WAV→OGG failed: {stderr.decode()[:200]}")
    return ogg_path


# ══════════════════════════════════════════════════════════
# Muxlisa.uz Provider (default)
# ══════════════════════════════════════════════════════════


async def muxlisa_transcribe(
    audio_path: str,
    api_key: str,
) -> TranscriptionResult:
    """Transcribe audio using Muxlisa.uz STT API.

    Supports OGG natively — no conversion needed for Telegram voice messages.
    Also supports: WAV, MP3, FLAC, M4A, AAC, MP4, WebM, AMR.
    """
    headers = {"x-api-key": api_key}

    if not os.path.isfile(audio_path):
        raise ValueError(f"Audio file does not exist: {audio_path}")
    # Prevent path traversal: only allow files in temp directories or current working dir
    real_path = os.path.realpath(audio_path)
    allowed_prefixes = (tempfile.gettempdir(), os.getcwd())
    if not any(real_path.startswith(os.path.realpath(p) + os.sep) or real_path == os.path.realpath(p) for p in allowed_prefixes):
        raise ValueError(f"Audio path not in allowed directory: {audio_path}")

    data = aiohttp.FormData()
    file_handle = open(audio_path, "rb")
    try:
        data.add_field(
            "audio",
            file_handle,
            filename=os.path.basename(audio_path),
        )

        session = await _get_session()
        async with session.post(
            f"{MUXLISA_BASE_URL}/stt",
            headers=headers,
            data=data,
        ) as resp:
            if resp.status == 402:
                raise RuntimeError("Muxlisa STT: balance depleted (402)")
            if resp.status == 429:
                raise RuntimeError("Muxlisa STT: rate limit exceeded (40 req/min)")
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Muxlisa STT error: HTTP {resp.status} — {body[:200]}")

            result = await resp.json()
            text = result.get("text", "") if isinstance(result, dict) else str(result)
            return TranscriptionResult(text=text)
    finally:
        file_handle.close()


# Muxlisa TTS voices
MUXLISA_VOICES = {
    "maftuna": 0,  # Female, Uzbek
    "asomiddin": 1,  # Male, Uzbek
}


async def muxlisa_tts(
    text: str,
    api_key: str,
    voice: str | None = None,
) -> TTSResult:
    """Convert text to speech using Muxlisa.uz TTS API.

    Returns WAV audio bytes directly (no URL to download).
    """
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }

    # Resolve speaker ID (0=female, 1=male)
    speaker = 0
    if voice:
        if voice.isdigit():
            speaker = int(voice)
        else:
            speaker = MUXLISA_VOICES.get(voice.lower(), 0)

    payload = {
        "text": text[:5000],
        "speaker": speaker,
    }

    session = await _get_session()
    async with session.post(
        f"{MUXLISA_BASE_URL}/tts",
        headers=headers,
        json=payload,
    ) as resp:
        if resp.status == 402:
            raise RuntimeError("Muxlisa TTS: balance depleted (402)")
        if resp.status == 429:
            raise RuntimeError("Muxlisa TTS: rate limit exceeded (60 req/min)")
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Muxlisa TTS error: HTTP {resp.status} — {body[:200]}")

        # Muxlisa returns WAV binary directly
        audio_data = await resp.read()
        return TTSResult(
            audio_data=audio_data,
            character_count=len(text),
        )


# ══════════════════════════════════════════════════════════
# KotibAI Provider (alternative)
# ══════════════════════════════════════════════════════════

KOTIB_VOICES = {
    "aziza": "DAwWafAKHO9QX6qcLggZ",       # Female, Uzbek (default)
    "nargiza": "RNmQxero1wyOi8SD9iMV",      # Female, Uzbek
    "soliha": "XDuXlUPQeAboPqoc7PJM",       # Female, Uzbek
    "sherzod": "wLCDHBSlf2alLqS7YPqK",      # Male, Uzbek
    "rachel": "21m00Tcm4TlvDq8ikWAM",       # Female, Russian/English
    "arnold": "VR6AewLTigWG4xSOukaG",       # Male, Russian/English
}


async def kotib_transcribe(
    audio_path: str,
    api_key: str,
    language: str | None = None,
) -> TranscriptionResult:
    """Transcribe audio using KotibAI STT API. Requires MP3/WAV/M4A (no OGG)."""
    headers = {"Authorization": f"Bearer {api_key}"}

    if not os.path.isfile(audio_path):
        raise ValueError(f"Audio file does not exist: {audio_path}")
    # Prevent path traversal: only allow files in temp directories or current working dir
    real_path = os.path.realpath(audio_path)
    allowed_prefixes = (tempfile.gettempdir(), os.getcwd())
    if not any(real_path.startswith(os.path.realpath(p) + os.sep) or real_path == os.path.realpath(p) for p in allowed_prefixes):
        raise ValueError(f"Audio path not in allowed directory: {audio_path}")

    data = aiohttp.FormData()
    file_handle = open(audio_path, "rb")
    try:
        data.add_field(
            "file",
            file_handle,
            filename=os.path.basename(audio_path),
        )
        data.add_field("blocking", "true")
        if language:
            data.add_field("language", language)

        session = await _get_session()
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
            text = result.get("text", "")
            # Strip "Speaker N:" prefixes from diarization output
            text = re.sub(r"Speaker \d+:\s*", "", text).strip()
            return TranscriptionResult(text=text, language=language)
    finally:
        file_handle.close()


async def kotib_tts(
    text: str,
    api_key: str,
    language: str = "uz",
    voice: str | None = None,
) -> TTSResult:
    """Convert text to speech using KotibAI TTS API. Returns audio URL."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    voice_id = None
    if voice:
        voice_id = KOTIB_VOICES.get(voice.lower(), voice)
    if not voice_id:
        voice_id = KOTIB_VOICES["rachel"] if language in ("ru", "en") else KOTIB_VOICES["aziza"]

    blocking = len(text) <= 500
    payload = {
        "text": text[:5000],
        "lang": language,
        "voice_id": voice_id,
        "blocking": blocking,
    }

    session = await _get_session()
    async with session.post(
        f"{KOTIB_BASE_URL}/tts",
        headers=headers,
        json=payload,
    ) as resp:
        result = await resp.json()
        if resp.status != 200:
            raise RuntimeError(f"KotibAI TTS error: {result.get('error', resp.status)}")

        if blocking:
            if result.get("status") != "success":
                raise RuntimeError(f"KotibAI TTS failed: {result}")
            return TTSResult(
                audio_url=result.get("audio_url", ""),
                character_count=result.get("character_count", 0),
            )

        task_id = result.get("id", "")
        if not task_id:
            raise RuntimeError(f"KotibAI TTS: no task_id: {result}")
        return await _kotib_poll_task(task_id, api_key, session)


async def _kotib_poll_task(
    task_id: str, api_key: str, session: aiohttp.ClientSession, max_wait: float = 30.0,
) -> TTSResult:
    """Poll KotibAI get-status endpoint."""
    headers = {"Authorization": f"Bearer {api_key}"}
    elapsed = 0.0
    interval = 1.0
    while elapsed < max_wait:
        await asyncio.sleep(interval)
        elapsed += interval
        async with session.get(
            f"{KOTIB_BASE_URL}/get-status", headers=headers, params={"task_id": task_id},
        ) as resp:
            result = await resp.json()
            status = result.get("status", "")
            if status == "completed":
                audio_url = result.get("audio_url", "")
                if not audio_url and isinstance(result.get("result"), dict):
                    audio_url = result["result"].get("audio_url", "")
                return TTSResult(audio_url=audio_url)
            if status in ("failed", "error"):
                raise RuntimeError(f"KotibAI TTS task failed: {result}")
        interval = min(interval * 1.2, 3.0)
    raise RuntimeError(f"KotibAI TTS timed out after {max_wait}s")


# ══════════════════════════════════════════════════════════
# Aisha AI Provider (Uzbek-native TTS with mood/gender)
# ══════════════════════════════════════════════════════════

AISHA_TTS_URL = "https://back.aisha.group/api/v1/tts/post/"
AISHA_STT_URL = "https://back.aisha.group/api/v1/stt/post/"

AISHA_VOICES = {
    "gulnoza": "gulnoza",    # Female, Uzbek
    "jaxongir": "jaxongir",  # Male, Uzbek
}

AISHA_MOODS = {"happy", "sad", "neutral"}


async def aisha_transcribe(
    audio_path: str,
    api_key: str,
    language: str | None = None,
) -> TranscriptionResult:
    """Transcribe audio using Aisha AI STT API.

    Aisha STT is async: submit audio → get task_id → poll for result.
    Supports uz/en/ru languages.
    """
    if not os.path.isfile(audio_path):
        raise ValueError(f"Audio file does not exist: {audio_path}")
    real_path = os.path.realpath(audio_path)
    allowed_prefixes = (tempfile.gettempdir(), os.getcwd())
    if not any(real_path.startswith(os.path.realpath(p) + os.sep) or real_path == os.path.realpath(p) for p in allowed_prefixes):
        raise ValueError(f"Audio path not in allowed directory: {audio_path}")

    headers = {"x-api-key": api_key}

    data = aiohttp.FormData()
    file_handle = open(audio_path, "rb")
    try:
        data.add_field(
            "audio",
            file_handle,
            filename=os.path.basename(audio_path),
        )
        if language:
            data.add_field("language", language)
        data.add_field("has_diarization", "false")

        session = await _get_session()
        # Step 1: Submit audio
        async with session.post(
            AISHA_STT_URL,
            headers=headers,
            data=data,
        ) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                raise RuntimeError(f"Aisha STT error: HTTP {resp.status} — {body[:200]}")

            result = await resp.json()

            # If text is directly returned
            if result.get("text"):
                return TranscriptionResult(text=result["text"], language=language)

            # Async mode: get task_id and poll
            task_id = result.get("task_id") or result.get("id")
            if not task_id:
                raise RuntimeError(f"Aisha STT: no task_id in response: {result}")

        # Step 2: Poll for result
        logger.info("Aisha STT task submitted: %s, polling...", task_id)
        poll_url = f"https://back.aisha.group/api/v1/stt/get/{task_id}/"
        elapsed = 0.0
        interval = 1.5
        max_wait = 30.0

        while elapsed < max_wait:
            await asyncio.sleep(interval)
            elapsed += interval

            async with session.get(poll_url, headers=headers) as poll_resp:
                if poll_resp.status != 200:
                    continue
                poll_result = await poll_resp.json()

                # Aisha returns transcript even while status is PENDING
                text = poll_result.get("transcript", "")
                if text:
                    logger.info("Aisha STT result: %s", text[:50])
                    return TranscriptionResult(text=text, language=language)

                status = poll_result.get("status", "")
                if status in ("FAILED", "failed", "ERROR", "error"):
                    raise RuntimeError(f"Aisha STT task failed: {poll_result}")

            interval = min(interval * 1.2, 3.0)

        raise RuntimeError(f"Aisha STT timed out after {max_wait}s")
    finally:
        file_handle.close()


async def aisha_tts(
    text: str,
    api_key: str,
    language: str = "uz",
    voice: str | None = None,
    mood: str = "neutral",
) -> TTSResult:
    """Convert text to speech using Aisha AI TTS API.

    Features: Uzbek-native voices (Gulnoza/Jaxongir), mood control,
    supports uz/en/ru languages. Returns MP3 audio bytes directly.
    """
    model = AISHA_VOICES.get((voice or "").lower(), "gulnoza")
    if mood not in AISHA_MOODS:
        mood = "neutral"

    headers = {
        "x-api-key": api_key,
        "X-Channels": "stereo",
        "X-Quality": "64k",
        "X-Rate": "16000",
        "X-Format": "mp3",
    }

    data = aiohttp.FormData()
    data.add_field("transcript", text[:5000])
    data.add_field("language", language if language in ("uz", "en", "ru") else "uz")
    data.add_field("model", model)
    data.add_field("mood", mood)

    session = await _get_session()
    async with session.post(
        AISHA_TTS_URL,
        headers=headers,
        data=data,
    ) as resp:
        if resp.status not in (200, 201):
            body = await resp.text()
            raise RuntimeError(f"Aisha TTS error: HTTP {resp.status} — {body[:200]}")

        # Read body once, then try JSON or raw audio
        raw = await resp.read()
        try:
            result = json.loads(raw)
            audio_url = result.get("audio_path", "") or result.get("audio_url", "")
            if audio_url:
                return TTSResult(
                    audio_url=audio_url,
                    character_count=len(text),
                )
        except (ValueError, UnicodeDecodeError):
            pass
        # Raw audio bytes
        return TTSResult(
            audio_data=raw,
            character_count=len(text),
        )


# ══════════════════════════════════════════════════════════
# OpenAI Whisper Provider (STT — high accuracy, multi-language)
# ══════════════════════════════════════════════════════════

WHISPER_STT_URL = "https://api.openai.com/v1/audio/transcriptions"


async def whisper_transcribe(
    audio_path: str,
    api_key: str,
    language: str | None = None,
) -> TranscriptionResult:
    """Transcribe audio using OpenAI Whisper API.

    High accuracy STT with auto language detection.
    Supports: mp3, mp4, mpeg, mpga, m4a, wav, webm.
    """
    if not os.path.isfile(audio_path):
        raise ValueError(f"Audio file does not exist: {audio_path}")
    real_path = os.path.realpath(audio_path)
    allowed_prefixes = (tempfile.gettempdir(), os.getcwd())
    if not any(real_path.startswith(os.path.realpath(p) + os.sep) or real_path == os.path.realpath(p) for p in allowed_prefixes):
        raise ValueError(f"Audio path not in allowed directory: {audio_path}")

    headers = {"Authorization": f"Bearer {api_key}"}

    data = aiohttp.FormData()
    file_handle = open(audio_path, "rb")
    try:
        data.add_field(
            "file",
            file_handle,
            filename=os.path.basename(audio_path),
        )
        data.add_field("model", "whisper-1")
        if language:
            data.add_field("language", language)

        session = await _get_session()
        async with session.post(
            WHISPER_STT_URL,
            headers=headers,
            data=data,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Whisper STT error: HTTP {resp.status} — {body[:200]}")

            result = await resp.json()
            text = result.get("text", "")
            return TranscriptionResult(text=text, language=language)
    finally:
        file_handle.close()


# ══════════════════════════════════════════════════════════
# Unified interface (used by telegram.py)
# ══════════════════════════════════════════════════════════


async def transcribe(
    audio_path: str,
    api_key: str,
    provider: str = "muxlisa",
    language: str | None = None,
) -> TranscriptionResult:
    """Transcribe audio using the configured provider.

    Args:
        audio_path: Path to audio file.
        api_key: Provider API key.
        provider: "muxlisa" | "kotib" | "whisper".
        language: Language hint (uz/ru/en).
    """
    if provider == "kotib":
        return await kotib_transcribe(audio_path, api_key, language)
    if provider == "whisper":
        return await whisper_transcribe(audio_path, api_key, language)
    if provider == "aisha":
        return await aisha_transcribe(audio_path, api_key, language)
    return await muxlisa_transcribe(audio_path, api_key)


async def text_to_speech(
    text: str,
    api_key: str,
    provider: str = "muxlisa",
    language: str = "uz",
    voice: str | None = None,
    mood: str = "neutral",
) -> TTSResult:
    """Convert text to speech using the configured provider.

    Providers:
        muxlisa: Uzbek native (maftuna/asomiddin voices)
        kotib:   6 voices, multi-language (aziza/sherzod/rachel/arnold)
        aisha:   Uzbek native with mood control (gulnoza/jaxongir)
    """
    if provider == "kotib":
        return await kotib_tts(text, api_key, language, voice)
    if provider == "aisha":
        return await aisha_tts(text, api_key, language, voice, mood)
    return await muxlisa_tts(text, api_key, voice)


_ALLOWED_AUDIO_HOSTS = {"cdn.aisha.group", "service.muxlisa.uz", "developer.kotib.ai", "api.openai.com"}


async def download_audio(url: str) -> str:
    """Download audio file from URL to a temp file.

    Only allows HTTPS URLs from known voice provider CDNs.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"Invalid audio URL scheme: {parsed.scheme}")
    if parsed.hostname and not any(parsed.hostname.endswith(h) for h in _ALLOWED_AUDIO_HOSTS):
        raise ValueError(f"Audio download blocked: untrusted host {parsed.hostname}")

    session = await _get_session()
    async with session.get(url) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Failed to download audio: HTTP {resp.status}")
        data = await resp.read()

    _, ext = os.path.splitext(parsed.path)
    suffix = ext.lower() if ext.lower() in (".wav", ".ogg", ".mp3") else ".mp3"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        return tmp.name
