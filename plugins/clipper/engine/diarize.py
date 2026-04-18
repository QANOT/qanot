"""Speaker diarization via pyannote — gated behind HuggingFace token.

Purpose:
  - Attach speaker labels to transcript words
  - Enables moment detector to bias toward single-speaker clips (less chaotic)
  - Enables reframer to track the currently-speaking person in multi-person shots

When to use:
  - Podcast / interview sources (≥2 speakers)
  - Not for vlogs, solo speakers, voiceovers (adds 30-90s runtime with no benefit)

Requires:
  - pyannote.audio >= 3.1.0
  - torch
  - HuggingFace token accepted for pyannote/speaker-diarization-3.1 gated model
  - HUGGINGFACE_TOKEN env var OR passed as param
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from engine.models import Transcript, Word

logger = logging.getLogger(__name__)

_pipeline = None


def _check_deps() -> tuple[bool, str]:
    missing: list[str] = []
    try:
        import pyannote.audio  # noqa: F401
    except ImportError:
        missing.append("pyannote.audio")
    try:
        import torch  # noqa: F401
    except ImportError:
        missing.append("torch")
    if missing:
        return False, f"diarization needs: pip install {' '.join(missing)}"
    return True, ""


def _get_pipeline(hf_token: str):
    """Lazy-load pyannote speaker diarization 3.1 pipeline."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    from pyannote.audio import Pipeline
    logger.info("Loading pyannote speaker-diarization-3.1...")
    _pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token,
    )
    return _pipeline


def _diarize_sync(
    audio_path: str,
    hf_token: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> list[tuple[float, float, str]]:
    """Blocking pyannote call. Returns [(start, end, speaker_id), ...]."""
    pipeline = _get_pipeline(hf_token)
    kwargs: dict = {}
    if min_speakers is not None:
        kwargs["min_speakers"] = min_speakers
    if max_speakers is not None:
        kwargs["max_speakers"] = max_speakers
    diarization = pipeline(audio_path, **kwargs)
    turns: list[tuple[float, float, str]] = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        turns.append((float(turn.start), float(turn.end), str(speaker)))
    return turns


def _assign_speakers_to_transcript(
    transcript: Transcript, turns: list[tuple[float, float, str]],
) -> Transcript:
    """Attach speaker labels to each word based on overlap with pyannote turns.

    For each word, find the speaker turn whose interval overlaps most with the
    word's midpoint.
    """
    if not turns:
        return transcript

    def speaker_at(mid: float) -> str | None:
        for start, end, spk in turns:
            if start <= mid <= end:
                return spk
        # No exact overlap — find nearest turn
        best_spk = None
        best_dist = float("inf")
        for start, end, spk in turns:
            dist = min(abs(mid - start), abs(mid - end))
            if dist < best_dist:
                best_dist = dist
                best_spk = spk
        return best_spk

    new_segments = []
    for seg in transcript.segments:
        new_words: list[Word] = []
        for w in seg.words:
            mid = (w.start_s + w.end_s) / 2
            new_words.append(Word(
                text=w.text,
                start_s=w.start_s,
                end_s=w.end_s,
                speaker=speaker_at(mid),
                confidence=w.confidence,
            ))
        # Segment speaker: most common word speaker
        if new_words:
            speaker_counts: dict[str, int] = {}
            for w in new_words:
                if w.speaker:
                    speaker_counts[w.speaker] = speaker_counts.get(w.speaker, 0) + 1
            seg_speaker = max(speaker_counts, key=speaker_counts.get) if speaker_counts else None
        else:
            seg_speaker = None
        new_segments.append(type(seg)(
            text=seg.text,
            start_s=seg.start_s,
            end_s=seg.end_s,
            words=new_words,
            speaker=seg_speaker,
        ))

    return type(transcript)(
        language=transcript.language,
        duration_s=transcript.duration_s,
        segments=new_segments,
    )


async def diarize(
    transcript: Transcript,
    audio_path: Path,
    *,
    hf_token: str | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> Transcript:
    """Attach speaker labels to a transcript via pyannote diarization.

    Returns the transcript unchanged on any failure (diarization is optional).
    """
    ok, msg = _check_deps()
    if not ok:
        logger.warning("Diarization skipped: %s", msg)
        return transcript

    if not hf_token:
        hf_token = os.environ.get("HUGGINGFACE_TOKEN")
    if not hf_token:
        logger.warning("Diarization skipped: no HUGGINGFACE_TOKEN (gated model)")
        return transcript

    try:
        turns = await asyncio.to_thread(
            _diarize_sync,
            str(audio_path),
            hf_token,
            min_speakers,
            max_speakers,
        )
    except Exception as e:
        logger.warning("Diarization failed (non-fatal): %s", e)
        return transcript

    num_speakers = len({t[2] for t in turns})
    logger.info("Diarization: %d speakers, %d turns", num_speakers, len(turns))
    return _assign_speakers_to_transcript(transcript, turns)


def summarize_speakers(transcript: Transcript) -> dict[str, float]:
    """Return speaker → total speaking seconds."""
    totals: dict[str, float] = {}
    for seg in transcript.segments:
        if seg.speaker:
            totals[seg.speaker] = totals.get(seg.speaker, 0.0) + (seg.end_s - seg.start_s)
    return totals
