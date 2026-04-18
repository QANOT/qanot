"""Video cutter + 9:16 reframe.

MVP implementation: accurate seek + center-crop. Smart face-tracking reframe
lives in reframer.py (Phase 2).

Design:
  - Use ffmpeg accurate seek (-ss AFTER -i) — slower than fast seek but
    frame-accurate, critical for matching LLM timestamps.
  - Re-encode (not stream copy) because we need to crop. H.264 yuv420p
    is the universally-compatible output for social platforms.
  - Audio: AAC 128k (Instagram Reels requirement).
  - Frame rate: pass-through (platforms accept 24-60 fps).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from engine.models import Moment, SourceMedia
from engine.source import _run

logger = logging.getLogger(__name__)


def _scale_crop_filter(src_w: int, src_h: int, target_w: int, target_h: int) -> str:
    """Build ffmpeg filter chain for center-crop to target aspect.

    Strategy:
      source is horizontal (16:9) → scale to fill target height, crop width
      source is vertical already  → scale to fill target width, crop height
      source matches aspect       → scale directly
    """
    src_aspect = src_w / max(src_h, 1)
    target_aspect = target_w / max(target_h, 1)

    if abs(src_aspect - target_aspect) < 0.01:
        # Already matching aspect — just scale
        return f"scale={target_w}:{target_h}"

    if src_aspect > target_aspect:
        # Source is wider than target — scale by height, crop width
        # e.g. 1920x1080 → 9:16 means scale to fit 1920 height, then center-crop width
        return (
            f"scale=-2:{target_h},"
            f"crop={target_w}:{target_h}:(iw-{target_w})/2:0"
        )
    else:
        # Source is taller than target — scale by width, crop height
        return (
            f"scale={target_w}:-2,"
            f"crop={target_w}:{target_h}:0:(ih-{target_h})/2"
        )


async def cut_clip(
    source: SourceMedia,
    moment: Moment,
    output_path: Path,
    *,
    target_width: int = 1080,
    target_height: int = 1920,
    reframe_mode: str = "center",
    crf: int = 20,
    preset: str = "medium",
) -> Path:
    """Cut a clip from source and apply reframe.

    Args:
        reframe_mode: "center" for MVP center-crop. "none" keeps original aspect
                      (letterboxes if needed). "smart" requires reframer.py (Phase 2).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    start = max(0.0, moment.start_s)
    duration = max(0.5, moment.end_s - moment.start_s)

    if reframe_mode == "none":
        # Keep source aspect — pad to target size to preserve orientation
        vf = (
            f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
            f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black"
        )
    elif reframe_mode == "center":
        vf = _scale_crop_filter(source.width, source.height, target_width, target_height)
    elif reframe_mode == "smart":
        # Phase 2 — delegate to reframer
        from engine.reframer import smart_reframe_cut
        return await smart_reframe_cut(
            source, moment, output_path,
            target_width=target_width, target_height=target_height,
            crf=crf, preset=preset,
        )
    else:
        raise ValueError(f"Unknown reframe_mode: {reframe_mode}")

    # Accurate seek + re-encode
    cmd: list[str] = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(source.path),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if source.has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ac", "2"])
    else:
        cmd.extend(["-an"])
    cmd.append(str(output_path))

    logger.info("Cutting clip: %.1fs-%.1fs (%.1fs) → %s", start, moment.end_s, duration, output_path.name)
    rc, out, err = await _run(*cmd, timeout=max(duration * 8, 60))
    if rc != 0:
        raise RuntimeError(f"ffmpeg cut failed: {err[-1000:]}")

    return output_path


async def extract_thumbnail(
    source: SourceMedia, moment: Moment, output_path: Path, *, width: int = 1080,
) -> Path:
    """Extract a thumbnail from the midpoint of a moment."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    midpoint = (moment.start_s + moment.end_s) / 2
    rc, out, err = await _run(
        "ffmpeg", "-y",
        "-ss", f"{midpoint:.3f}",
        "-i", str(source.path),
        "-vframes", "1",
        "-vf", f"scale={width}:-2",
        "-q:v", "2",
        str(output_path),
        timeout=30.0,
    )
    if rc != 0:
        raise RuntimeError(f"Thumbnail extraction failed: {err[:500]}")
    return output_path
