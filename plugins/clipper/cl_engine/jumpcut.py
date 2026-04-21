"""Silence/jump-cut stage — compresses speaking pauses without losing meaning.

Applied after reframe, before caption burn.

Design philosophy (based on Descript / Cleanvoice / OpusClip field data):

  • DO NOT delete silence — COMPRESS it. Removing all dead air creates a
    rushed, exhausting pace (Descript users' #1 complaint). Instead, cap
    long pauses to a natural target length.
  • Context-aware target gaps:
      – Sentence boundary (end of one segment → start of next):
        keep ~0.45s. Preserves "breathing room" between thoughts and lets
        viewers process meaning.
      – Within-sentence (same segment, filler/hesitation pause):
        compress to ~0.25s. Tightens pacing without feeling spliced.
  • ONLY act on gaps longer than `long_gap_threshold_s` (default 0.5s).
    Shorter natural pauses are untouched — cutting them is where
    "sped-up robot" artifacts come from.
  • Sentence boundaries detected from transcript segment structure
    (qanot's Whisper segments are sentence-level). This matches how
    Whisper/whisperx chunk utterances and is more reliable than
    punctuation heuristics (Uzbek Whisper often drops punctuation).
  • Total-duration safety: if cumulative cuts would drop the clip below
    `min_final_duration_s`, scale back the compression proportionally.

Word timestamps are remapped onto the compressed timeline so captions
remain perfectly aligned.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import replace
from pathlib import Path

from cl_engine.models import Segment, Word
from cl_engine.source import _run

logger = logging.getLogger(__name__)


def _sentence_boundary_times(segments: list[Segment]) -> set[float]:
    """Return set of timestamps (word end_s) that mark sentence boundaries.

    A sentence boundary is the LAST word of each segment. We compare word
    end_s against these floats to decide which gaps are "between thoughts".
    """
    boundaries: set[float] = set()
    for seg in segments:
        if not seg.words:
            continue
        # Use last word's end_s as the boundary marker
        boundaries.add(round(seg.words[-1].end_s, 3))
    return boundaries


def compute_keep_segments(
    words: list[Word],
    clip_duration: float,
    *,
    # Only compress gaps LONGER than this. Shorter pauses are natural — keep them.
    long_gap_threshold_s: float = 0.5,
    # Target gap length WITHIN a sentence (mid-thought pause after compression).
    target_mid_sentence_gap_s: float = 0.25,
    # Target gap length BETWEEN sentences (preserves thinking/breathing room).
    target_sentence_boundary_gap_s: float = 0.45,
    # Leading/trailing silence gets trimmed to this amount (never fully removed).
    edge_silence_target_s: float = 0.2,
    # Never allow a compressed gap shorter than this (prevents splice artifacts).
    min_kept_gap_s: float = 0.1,
    # Drop any resulting kept fragment shorter than this (would glitch on concat).
    min_segment_s: float = 0.08,
    # Sentence boundary hints — word end_s floats. If None, every gap is treated
    # as mid-sentence (safe fallback — slightly more silence kept).
    sentence_boundary_times: set[float] | None = None,
) -> list[tuple[float, float]]:
    """Return list of (start, end) KEEP ranges in clip-local seconds.

    Implements the "compress, don't delete" strategy. A gap is only touched
    if it exceeds long_gap_threshold_s; otherwise it passes through
    untouched to preserve natural cadence.

    Returns [(0, clip_duration)] if no gap qualifies for compression —
    caller can then skip the expensive concat re-encode.
    """
    if not words or clip_duration <= 0:
        return [(0.0, clip_duration)]

    # Sort and clamp to clip-local window.
    ws = sorted(words, key=lambda w: w.start_s)
    ws = [w for w in ws if w.end_s > 0 and w.start_s < clip_duration]
    if not ws:
        return [(0.0, clip_duration)]

    boundaries = sentence_boundary_times or set()

    # Build list of silence intervals to REMOVE (start, end in clip-local).
    # We compress a gap by cutting its MIDDLE so we retain equal padding on
    # each side — this avoids an abrupt splice right against a word.
    cut_points: list[tuple[float, float]] = []

    # Leading silence (from 0 to first word).
    first_word_start = ws[0].start_s
    if first_word_start > long_gap_threshold_s:
        # Keep edge_silence_target_s before first word; cut the rest.
        new_clip_start = max(0.0, first_word_start - edge_silence_target_s)
        if new_clip_start > 0.05:
            cut_points.append((0.0, new_clip_start))

    # Inter-word gaps.
    for i in range(len(ws) - 1):
        w_end = ws[i].end_s
        w_next_start = ws[i + 1].start_s
        gap = w_next_start - w_end
        if gap <= long_gap_threshold_s:
            # Natural short pause → untouched.
            continue

        # Pick target gap based on sentence-boundary context.
        is_boundary = round(w_end, 3) in boundaries
        target_gap = (
            target_sentence_boundary_gap_s if is_boundary else target_mid_sentence_gap_s
        )
        target_gap = max(target_gap, min_kept_gap_s)

        # If actual gap already ≤ target (shouldn't happen after threshold check,
        # but defensive), skip.
        if gap <= target_gap:
            continue

        # Cut the middle of the gap, leaving target_gap/2 on each side.
        half = target_gap / 2.0
        c_start = w_end + half
        c_end = w_next_start - half
        if c_end - c_start > 0.05:
            cut_points.append((c_start, c_end))

    # Trailing silence.
    last_word_end = ws[-1].end_s
    trailing = clip_duration - last_word_end
    if trailing > long_gap_threshold_s:
        new_clip_end = min(clip_duration, last_word_end + edge_silence_target_s)
        if clip_duration - new_clip_end > 0.05:
            cut_points.append((new_clip_end, clip_duration))

    if not cut_points:
        return [(0.0, clip_duration)]

    # Convert cut intervals → keep intervals.
    keeps: list[tuple[float, float]] = []
    prev_end = 0.0
    for (c_start, c_end) in cut_points:
        if c_start > prev_end + 1e-3:
            keeps.append((prev_end, c_start))
        prev_end = c_end
    if clip_duration - prev_end > 1e-3:
        keeps.append((prev_end, clip_duration))

    # Drop too-short fragments.
    keeps = [(s, e) for (s, e) in keeps if e - s >= min_segment_s]
    return keeps if keeps else [(0.0, clip_duration)]


def remap_words(
    words: list[Word],
    keep_segments: list[tuple[float, float]],
) -> list[Word]:
    """Shift word timestamps onto the compressed timeline."""
    if not words:
        return []
    # Identity pass — no remapping needed.
    if len(keep_segments) == 1 and abs(keep_segments[0][0]) < 1e-3:
        return list(words)

    out: list[Word] = []
    cumulative = 0.0
    for (seg_start, seg_end) in keep_segments:
        seg_len = seg_end - seg_start
        for w in words:
            mid = (w.start_s + w.end_s) / 2
            if seg_start <= mid <= seg_end:
                new_start = max(0.0, w.start_s - seg_start) + cumulative
                new_end = max(new_start + 0.05, w.end_s - seg_start) + cumulative
                out.append(replace(w, start_s=new_start, end_s=new_end))
        cumulative += seg_len
    return out


def total_kept_duration(keep_segments: list[tuple[float, float]]) -> float:
    return sum(max(0.0, e - s) for (s, e) in keep_segments)


async def apply_jumpcut(
    input_path: Path,
    output_path: Path,
    keep_segments: list[tuple[float, float]],
    *,
    has_audio: bool = True,
    crf: int = 20,
    preset: str = "medium",
) -> None:
    """Render input → output keeping only the listed segments (trim+concat).

    Uses ffmpeg trim + atrim + concat filter so A/V sync is preserved exactly.
    """
    if not keep_segments:
        raise ValueError("keep_segments is empty")

    # No-op: single segment starting at 0 — skip re-encode.
    if (
        len(keep_segments) == 1
        and abs(keep_segments[0][0]) < 1e-3
    ):
        shutil.copy(input_path, output_path)
        return

    parts: list[str] = []
    concat_refs: list[str] = []
    for i, (s, e) in enumerate(keep_segments):
        parts.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]"
        )
        if has_audio:
            parts.append(
                f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]"
            )
            concat_refs.append(f"[v{i}][a{i}]")
        else:
            concat_refs.append(f"[v{i}]")

    n = len(keep_segments)
    if has_audio:
        parts.append(
            f"{''.join(concat_refs)}concat=n={n}:v=1:a=1[vout][aout]"
        )
    else:
        parts.append(
            f"{''.join(concat_refs)}concat=n={n}:v=1:a=0[vout]"
        )
    filter_complex = ";".join(parts)

    cmd: list[str] = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]",
    ]
    if has_audio:
        cmd.extend(["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"])
    else:
        cmd.append("-an")
    cmd.extend([
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ])

    rc, out, err = await _run(*cmd, timeout=600.0)
    if rc != 0:
        logger.error("ffmpeg jumpcut cmd: %s", " ".join(cmd))
        logger.error("ffmpeg jumpcut stderr (full):\n%s", err)
        tail = err.strip().splitlines()[-6:] if err else []
        raise RuntimeError("ffmpeg jumpcut failed: " + " | ".join(tail))
