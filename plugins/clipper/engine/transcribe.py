"""Transcription stage — faster-whisper local with word-level timestamps.

Design:
  - Default: faster-whisper large-v3 with word_timestamps=True (no external API)
  - Optional: ElevenLabs Scribe for higher accuracy in Uzbek/99 languages
  - Optional: WhisperX alignment pass for sub-word precision

All model loads are lazy — the first call blocks on model download.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from engine.models import Segment, SourceMedia, Transcript, Word
from engine.source import extract_audio

logger = logging.getLogger(__name__)

# Module-level cache for the faster-whisper model.
# Loading takes 2-5 seconds and 1-3 GB RAM — reuse across calls.
_whisper_model = None
_whisper_model_name: str | None = None
_whisper_compute: str | None = None


def _get_whisper_model(model_name: str, compute_type: str):
    """Lazy-load and cache the faster-whisper model."""
    global _whisper_model, _whisper_model_name, _whisper_compute
    if (
        _whisper_model is not None
        and _whisper_model_name == model_name
        and _whisper_compute == compute_type
    ):
        return _whisper_model
    from faster_whisper import WhisperModel
    logger.info("Loading faster-whisper model %s (compute=%s)...", model_name, compute_type)
    _whisper_model = WhisperModel(model_name, compute_type=compute_type)
    _whisper_model_name = model_name
    _whisper_compute = compute_type
    return _whisper_model


def _transcribe_sync(
    audio_path: str,
    model_name: str,
    compute_type: str,
    language: str,
) -> Transcript:
    """Blocking transcription — call from asyncio.to_thread."""
    model = _get_whisper_model(model_name, compute_type)
    segments_iter, info = model.transcribe(
        audio_path,
        language=language if language != "auto" else None,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )

    segments: list[Segment] = []
    for s in segments_iter:
        words: list[Word] = []
        for w in (s.words or []):
            # faster-whisper returns words with .word (the raw text with leading space)
            text = w.word.strip()
            if not text:
                continue
            words.append(Word(
                text=text,
                start_s=float(w.start or 0.0),
                end_s=float(w.end or 0.0),
                confidence=float(getattr(w, "probability", 1.0) or 1.0),
            ))
        segments.append(Segment(
            text=s.text.strip(),
            start_s=float(s.start or 0.0),
            end_s=float(s.end or 0.0),
            words=words,
        ))

    return Transcript(
        language=info.language,
        duration_s=float(info.duration or 0.0),
        segments=segments,
    )


async def transcribe_fasterwhisper(
    source: SourceMedia,
    *,
    model_name: str = "large-v3",
    compute_type: str = "int8",
    language: str = "uz",
    work_dir: Path | None = None,
) -> Transcript:
    """Transcribe using faster-whisper (local, no API costs)."""
    work_dir = work_dir or source.path.parent
    audio_path = work_dir / f"{source.path.stem}.wav"
    if not audio_path.exists():
        await extract_audio(source, audio_path)

    return await asyncio.to_thread(
        _transcribe_sync,
        str(audio_path),
        model_name,
        compute_type,
        language,
    )


async def transcribe_elevenlabs(
    source: SourceMedia,
    *,
    api_key: str,
    language: str = "uzb",  # ISO 639-3 for Uzbek in Scribe
    work_dir: Path | None = None,
) -> Transcript:
    """Transcribe using ElevenLabs Scribe (paid, higher accuracy for non-English).

    Requires ELEVENLABS_API_KEY. Uses /v1/speech-to-text endpoint with word timestamps.
    """
    import aiohttp

    work_dir = work_dir or source.path.parent
    audio_path = work_dir / f"{source.path.stem}.wav"
    if not audio_path.exists():
        await extract_audio(source, audio_path)

    # ElevenLabs Scribe endpoint
    url = "https://api.elevenlabs.io/v1/speech-to-text"
    headers = {"xi-api-key": api_key}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as session:
        with audio_path.open("rb") as f:
            data = aiohttp.FormData()
            data.add_field("file", f, filename=audio_path.name, content_type="audio/wav")
            data.add_field("model_id", "scribe_v1")
            data.add_field("language_code", language)
            data.add_field("timestamps_granularity", "word")
            async with session.post(url, data=data, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"ElevenLabs Scribe failed: HTTP {resp.status}: {body[:500]}")
                result = await resp.json()

    # Scribe response format: {"language_code": "...", "text": "...", "words": [{"text","start","end","type",...}]}
    words_raw = result.get("words", [])
    all_words: list[Word] = []
    for w in words_raw:
        if w.get("type") != "word":
            continue
        all_words.append(Word(
            text=str(w.get("text", "")).strip(),
            start_s=float(w.get("start", 0.0)),
            end_s=float(w.get("end", 0.0)),
            speaker=w.get("speaker_id"),
        ))

    # Group words into sentence-level segments (heuristic: pause > 0.5s or sentence-ending punct)
    segments: list[Segment] = []
    current: list[Word] = []
    for i, w in enumerate(all_words):
        current.append(w)
        is_sentence_end = w.text.endswith((".", "!", "?"))
        has_pause = (
            i + 1 < len(all_words)
            and all_words[i + 1].start_s - w.end_s > 0.5
        )
        if is_sentence_end or has_pause or i == len(all_words) - 1:
            segments.append(Segment(
                text=" ".join(x.text for x in current),
                start_s=current[0].start_s,
                end_s=current[-1].end_s,
                words=current,
            ))
            current = []

    return Transcript(
        language=result.get("language_code", language),
        duration_s=source.duration_s,
        segments=segments,
    )


def _align_with_whisperx_sync(
    transcript: Transcript, audio_path: str, language: str, device: str = "cpu"
) -> Transcript:
    """Refine word timestamps using WhisperX forced alignment."""
    try:
        import whisperx
    except ImportError:
        logger.warning("whisperx not installed — skipping alignment")
        return transcript

    try:
        align_model, metadata = whisperx.load_align_model(language_code=language, device=device)
    except Exception as e:
        logger.warning("WhisperX alignment model load failed for %s: %s", language, e)
        return transcript

    # Convert our segments to WhisperX's expected format
    wx_segments = [
        {"text": s.text, "start": s.start_s, "end": s.end_s}
        for s in transcript.segments
    ]

    result = whisperx.align(wx_segments, align_model, metadata, audio_path, device)

    # Build new segments with aligned words
    new_segments: list[Segment] = []
    for i, wx_seg in enumerate(result.get("segments", [])):
        orig = transcript.segments[i] if i < len(transcript.segments) else None
        words: list[Word] = []
        for w in wx_seg.get("words", []):
            words.append(Word(
                text=str(w.get("word", "")).strip(),
                start_s=float(w.get("start", 0.0)),
                end_s=float(w.get("end", 0.0)),
                confidence=float(w.get("score", 1.0)),
                speaker=orig.speaker if orig else None,
            ))
        new_segments.append(Segment(
            text=wx_seg.get("text", orig.text if orig else ""),
            start_s=float(wx_seg.get("start", 0.0)),
            end_s=float(wx_seg.get("end", 0.0)),
            words=words,
            speaker=orig.speaker if orig else None,
        ))

    return Transcript(
        language=transcript.language,
        duration_s=transcript.duration_s,
        segments=new_segments,
    )


async def align_words(transcript: Transcript, audio_path: Path, language: str) -> Transcript:
    """Run WhisperX forced alignment pass for precise word timestamps."""
    return await asyncio.to_thread(
        _align_with_whisperx_sync,
        transcript,
        str(audio_path),
        language,
    )


async def transcribe(
    source: SourceMedia,
    *,
    provider: str = "faster-whisper",
    model_name: str = "large-v3",
    compute_type: str = "int8",
    language: str = "uz",
    align: bool = False,
    elevenlabs_key: str | None = None,
    work_dir: Path | None = None,
) -> Transcript:
    """Transcribe a source video to a Transcript with word timestamps.

    Args:
        provider: "faster-whisper" (local) or "elevenlabs" (API).
        language: ISO 639-1 for faster-whisper, ISO 639-3 for ElevenLabs.
        align: Run WhisperX alignment pass for sub-word precision.
    """
    if provider == "elevenlabs":
        if not elevenlabs_key:
            elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY")
        if not elevenlabs_key:
            raise RuntimeError("ElevenLabs provider requires elevenlabs_key or ELEVENLABS_API_KEY env var")
        # Scribe uses ISO 639-3 codes — map common ones
        lang_map = {"uz": "uzb", "en": "eng", "ru": "rus", "tr": "tur"}
        iso3 = lang_map.get(language, language)
        transcript = await transcribe_elevenlabs(source, api_key=elevenlabs_key, language=iso3, work_dir=work_dir)
    else:
        transcript = await transcribe_fasterwhisper(
            source,
            model_name=model_name,
            compute_type=compute_type,
            language=language,
            work_dir=work_dir,
        )

    if align:
        work_dir = work_dir or source.path.parent
        audio_path = work_dir / f"{source.path.stem}.wav"
        try:
            transcript = await align_words(transcript, audio_path, language)
        except Exception as e:
            logger.warning("Word alignment failed (non-fatal): %s", e)

    return transcript
