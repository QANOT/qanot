"""Hook overlay — renders oversized text across first 3 seconds of each clip.

This is the "thumb-stopper" that OpusClip/Submagic add before any captions.
It's a different visual from the word-by-word captions — full-screen bold text
with a dark scrim, designed to be readable even with sound off.

Pipeline position: runs AFTER reframe but BEFORE caption burn.
Result: clip starts with BIG hook text → transitions into word-by-word captions.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from cl_engine.captions import _get_font
from cl_engine.source import _run

logger = logging.getLogger(__name__)

# Hook visual parameters
HOOK_DURATION_S = 2.5  # shown from t=0 to t=2.5s
HOOK_FADE_OUT_S = 0.5  # fade out over last 0.5s
HOOK_FONT_SIZE = 110
HOOK_STROKE = 6
HOOK_MAX_LINE_CHARS = 18  # wrap after this many chars per line


def _wrap_text(text: str, max_chars: int) -> list[str]:
    """Word-wrap text to lines of max_chars. Preserves words."""
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    current_len = 0
    for w in words:
        # +1 for space
        added = len(w) + (1 if current else 0)
        if current_len + added > max_chars and current:
            lines.append(" ".join(current))
            current = [w]
            current_len = len(w)
        else:
            current.append(w)
            current_len += added
    if current:
        lines.append(" ".join(current))
    return lines


def _render_hook_png(
    hook_text: str,
    canvas_width: int,
    canvas_height: int,
) -> Image.Image:
    """Render the hook overlay — dark scrim + big bold text."""
    # Transparent canvas (ffmpeg overlay will composite)
    img = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))

    # Top dark scrim (gradient from top) to improve readability
    scrim_height = int(canvas_height * 0.55)
    scrim = Image.new("RGBA", (canvas_width, scrim_height), (0, 0, 0, 0))
    scrim_draw = ImageDraw.Draw(scrim)
    for y in range(scrim_height):
        alpha = int(180 * (1 - y / scrim_height) ** 1.5)
        scrim_draw.line([(0, y), (canvas_width, y)], fill=(0, 0, 0, alpha))
    img.paste(scrim, (0, 0), scrim)

    # Text
    font = _get_font(HOOK_FONT_SIZE)
    draw = ImageDraw.Draw(img)
    lines = _wrap_text(hook_text.upper(), HOOK_MAX_LINE_CHARS)

    # Measure total height
    line_heights: list[int] = []
    line_widths: list[int] = []
    for line in lines:
        bbox = font.getbbox(line)
        line_widths.append(bbox[2] - bbox[0])
        line_heights.append(bbox[3] - bbox[1])

    line_spacing = int(HOOK_FONT_SIZE * 0.3)
    total_h = sum(line_heights) + line_spacing * max(0, len(lines) - 1)

    # Position at 25% down (top-third sweet spot)
    start_y = int(canvas_height * 0.22 - total_h / 2)
    y = start_y
    for i, line in enumerate(lines):
        x = (canvas_width - line_widths[i]) // 2
        # Black stroke outline for readability on any background
        draw.text(
            (x, y), line,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=HOOK_STROKE,
            stroke_fill=(0, 0, 0, 255),
        )
        y += line_heights[i] + line_spacing

    return img


async def burn_hook_overlay(
    clip_path: Path,
    hook_text: str,
    output_path: Path,
    *,
    canvas_width: int = 1080,
    canvas_height: int = 1920,
    duration_s: float = HOOK_DURATION_S,
) -> Path:
    """Burn an animated hook overlay onto the first N seconds of a clip.

    The hook fades out over the last HOOK_FADE_OUT_S seconds.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not installed")

    if not hook_text or not hook_text.strip():
        # No hook — copy clip unchanged
        shutil.copy(clip_path, output_path)
        return output_path

    work_dir = clip_path.parent / f".hook_{clip_path.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    png_path = work_dir / "hook.png"

    try:
        img = _render_hook_png(hook_text, canvas_width, canvas_height)
        img.save(png_path, "PNG")

        fade_start = max(0.0, duration_s - HOOK_FADE_OUT_S)
        # enable window 0..duration_s; alpha fade 1.0→0.0 over last HOOK_FADE_OUT_S seconds
        # Use format=rgba to preserve alpha channel during fade.
        overlay_expr = (
            f"[0:v][1:v]overlay=0:0:"
            f"enable='between(t,0,{duration_s})':"
            f"format=auto[v]"
        )
        # Alpha fade on the png input
        fade_filter = (
            f"[1:v]fade=t=out:st={fade_start:.2f}:d={HOOK_FADE_OUT_S:.2f}:alpha=1[hook]"
        )
        filter_complex = f"{fade_filter};[0:v][hook]overlay=0:0:enable='between(t,0,{duration_s:.2f})'[v]"

        cmd = [
            "ffmpeg", "-y",
            "-i", str(clip_path),
            "-loop", "1",
            "-t", f"{duration_s:.2f}",
            "-i", str(png_path),
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "0:a?",
            "-c:a", "copy",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path),
        ]
        rc, out, err = await _run(*cmd, timeout=300.0)
        if rc != 0:
            logger.warning("Hook overlay ffmpeg failed — copying without overlay: %s", err[-500:])
            shutil.copy(clip_path, output_path)
            return output_path

        return output_path
    finally:
        try:
            shutil.rmtree(work_dir)
        except OSError:
            pass
