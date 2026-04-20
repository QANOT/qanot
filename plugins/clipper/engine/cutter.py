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
        return f"scale={target_w}:{target_h}"

    if src_aspect > target_aspect:
        return (
            f"scale=-2:{target_h},"
            f"crop={target_w}:{target_h}:(iw-{target_w})/2:0"
        )
    else:
        return (
            f"scale={target_w}:-2,"
            f"crop={target_w}:{target_h}:0:(ih-{target_h})/2"
        )


def _blur_pad_filter(src_w: int, src_h: int, target_w: int, target_h: int) -> str:
    """Blur-background composite (OpusClip/Submagic/Vugola style).

    Preserves 100% of the original frame — scales the original to fit target
    width (or height, whichever is limiting), centers it, and fills the
    remaining space with a blurred copy of the same video scaled to fill 9:16.

    Result: clear un-cropped original in the middle, blurred background
    filling the rest. No edge content loss. Professional look.

    Uses filter_complex with split + two pipelines (scale+blur for bg,
    scale-to-fit for fg) then overlay centered.
    """
    # Foreground: scale so the entire source fits within target — use
    # `force_original_aspect_ratio=decrease` so we get "contain" semantics.
    # Resulting dimensions are at most target_w × target_h.
    fg_chain = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease"
    )

    # Background: scale to fill entirely, then crop to exact target, then blur.
    # `force_original_aspect_ratio=increase` = cover semantics.
    bg_chain = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},"
        f"boxblur=luma_radius=30:luma_power=2:"
        f"chroma_radius=30:chroma_power=2"
    )

    # Split input, process both branches, overlay FG centered over BG.
    # NOTE: the leading [0:v] label is required when this is fed to
    # -filter_complex (labeled graph). cut_clip() detects the leading
    # bracket and switches from -vf to -filter_complex automatically.
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]{bg_chain}[bgblur];"
        f"[fg]{fg_chain}[fgfit];"
        f"[bgblur][fgfit]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]"
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
        reframe_mode:
            "blur_pad"  — PRODUCTION DEFAULT. Original shown uncropped with
                          blurred-background fill (OpusClip/Submagic/Vugola
                          style). No content loss, works for content with
                          text/graphics at the edges.
            "center"    — Naive center-crop. Loses left/right edges (content
                          in edges like lower-thirds, text overlays disappears).
                          Fast, simple, but destructive.
            "none"      — Letterbox with black bars. Ugly but preserves all
                          content.
            "smart"     — Face-tracked reframe (requires MediaPipe + YOLOv8).
                          Good for solo talking head; still loses edge content.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    start = max(0.0, moment.start_s)
    duration = max(0.5, moment.end_s - moment.start_s)

    if reframe_mode == "none":
        vf = (
            f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
            f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black"
        )
    elif reframe_mode == "center":
        vf = _scale_crop_filter(source.width, source.height, target_width, target_height)
    elif reframe_mode in ("blur_pad", "blurpad", "blur"):
        vf = _blur_pad_filter(source.width, source.height, target_width, target_height)
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

    # If the filter graph uses labels ([bg], [fg], [vout], …) it must be
    # fed to -filter_complex, not -vf. Detect by leading bracket.
    uses_complex = vf.lstrip().startswith("[")

    cmd: list[str] = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(source.path),
        "-t", f"{duration:.3f}",
    ]
    if uses_complex:
        cmd.extend(["-filter_complex", vf, "-map", "[vout]"])
        if source.has_audio:
            cmd.extend(["-map", "0:a?"])
    else:
        cmd.extend(["-vf", vf])

    cmd.extend([
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ])
    if source.has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ac", "2"])
    else:
        cmd.extend(["-an"])
    cmd.append(str(output_path))

    logger.info(
        "Cutting clip: %.1fs-%.1fs (%.1fs) → %s (mode=%s, complex=%s)",
        start, moment.end_s, duration, output_path.name, reframe_mode, uses_complex,
    )
    rc, out, err = await _run(*cmd, timeout=max(duration * 8, 60))
    if rc != 0:
        # Log the full command + stderr so we can actually diagnose. Previous
        # err[-1000:] was silently chopping the real failure line.
        logger.error("ffmpeg cut cmd: %s", " ".join(cmd))
        logger.error("ffmpeg cut stderr (full):\n%s", err)
        tail = err.strip().splitlines()[-8:] if err else []
        raise RuntimeError("ffmpeg cut failed: " + " | ".join(tail))

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
