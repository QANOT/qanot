"""Caption renderer — Captions.ai / Submagic style word-by-word highlighting.

Design:
  - Pillow renders one transparent PNG per (page, active_word) state.
  - FFmpeg overlay filter composites PNGs onto the clip at their time ranges.
  - Fully self-contained — no MoviePy, no Remotion. Just Pillow + ffmpeg.

Caption styles:
  - captions_ai: 4 words/page, gold box around active word, white text
  - submagic: 1-2 words/page, huge, neon/yellow active, scale-up animation
  - minimal: single sentence at bottom, no highlighting, small font

Cyrillic + Latin supported via Montserrat/DejaVu fonts.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from engine.models import Word

logger = logging.getLogger(__name__)

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

PLUGIN_DIR = Path(__file__).parent.parent
ASSETS_DIR = PLUGIN_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"


@dataclass
class CaptionStyle:
    """Visual style for captions."""
    name: str
    words_per_page: int
    font_size: int
    y_position_pct: float  # 0.0 = top, 1.0 = bottom
    text_color: tuple[int, int, int, int]  # RGBA
    active_bg: tuple[int, int, int, int]
    active_text: tuple[int, int, int, int]
    stroke_color: tuple[int, int, int, int]
    stroke_width: int
    padding_x: int
    padding_y: int
    line_height_multiplier: float = 1.2


STYLES: dict[str, CaptionStyle] = {
    "captions_ai": CaptionStyle(
        name="captions_ai",
        words_per_page=4,
        font_size=84,
        y_position_pct=0.65,
        text_color=(255, 255, 255, 255),
        active_bg=(255, 195, 0, 255),  # gold
        active_text=(30, 30, 30, 255),
        stroke_color=(0, 0, 0, 255),
        stroke_width=4,
        padding_x=18,
        padding_y=10,
    ),
    "submagic": CaptionStyle(
        name="submagic",
        words_per_page=2,
        font_size=120,
        y_position_pct=0.55,
        text_color=(255, 255, 255, 255),
        active_bg=(0, 0, 0, 0),  # no bg, color swap only
        active_text=(255, 230, 0, 255),  # neon yellow
        stroke_color=(0, 0, 0, 255),
        stroke_width=6,
        padding_x=0,
        padding_y=0,
    ),
    "minimal": CaptionStyle(
        name="minimal",
        words_per_page=8,
        font_size=54,
        y_position_pct=0.85,
        text_color=(255, 255, 255, 255),
        active_bg=(0, 0, 0, 0),
        active_text=(255, 255, 255, 255),
        stroke_color=(0, 0, 0, 255),
        stroke_width=3,
        padding_x=0,
        padding_y=0,
    ),
}


_FONT_CANDIDATES: list[Path] = [
    FONTS_DIR / "Montserrat-ExtraBold.ttf",
    FONTS_DIR / "Montserrat-Bold.ttf",
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),  # macOS
    Path("/System/Library/Fonts/Helvetica.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),  # Linux
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/Library/Fonts/Arial Bold.ttf"),
]


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    """Load a bold font supporting Cyrillic + Latin. Cached."""
    key = ("default", size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for candidate in _FONT_CANDIDATES:
        if candidate.exists():
            try:
                font = ImageFont.truetype(str(candidate), size)
                _FONT_CACHE[key] = font
                return font
            except Exception:
                continue
    logger.warning("No TrueType font found — falling back to default (poor quality)")
    font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def clip_local_words(words: list[Word], clip_start_s: float, clip_end_s: float) -> list[Word]:
    """Translate source-timed words into clip-local time, filtering to the range."""
    result: list[Word] = []
    for w in words:
        # Keep words whose midpoint falls within the clip range
        mid = (w.start_s + w.end_s) / 2
        if mid < clip_start_s or mid > clip_end_s:
            continue
        local_start = max(0.0, w.start_s - clip_start_s)
        local_end = max(local_start + 0.05, w.end_s - clip_start_s)
        result.append(Word(
            text=w.text,
            start_s=local_start,
            end_s=local_end,
            speaker=w.speaker,
            confidence=w.confidence,
        ))
    return result


def _build_pages(words: list[Word], words_per_page: int) -> list[list[Word]]:
    """Split word list into pages."""
    if words_per_page <= 0:
        return [words]
    pages: list[list[Word]] = []
    for i in range(0, len(words), words_per_page):
        pages.append(words[i : i + words_per_page])
    return pages


def _measure_text(font: ImageFont.FreeTypeFont, text: str) -> tuple[int, int]:
    """Return (width, height) of rendered text."""
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _render_page_png(
    page: list[Word],
    active_index: int,
    style: CaptionStyle,
    canvas_width: int,
    canvas_height: int,
) -> Image.Image:
    """Render one caption state (page + which word is active) as transparent PNG."""
    img = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _get_font(style.font_size)

    # Measure each word + spaces
    gap = int(style.font_size * 0.25)
    word_sizes: list[tuple[int, int]] = []
    for w in page:
        ww, wh = _measure_text(font, w.text)
        word_sizes.append((ww, wh))

    # Wrap to multi-line if total width exceeds canvas
    max_line_width = int(canvas_width * 0.88)
    lines: list[list[int]] = [[]]
    cur_w = 0
    for idx, (ww, _) in enumerate(word_sizes):
        word_full_w = ww + style.padding_x * 2
        if cur_w + word_full_w + (gap if cur_w > 0 else 0) > max_line_width and lines[-1]:
            lines.append([idx])
            cur_w = word_full_w
        else:
            lines[-1].append(idx)
            cur_w += word_full_w + (gap if len(lines[-1]) > 1 else 0)

    # Vertical layout
    line_height = int(style.font_size * style.line_height_multiplier)
    total_h = line_height * len(lines)
    baseline_y = int(canvas_height * style.y_position_pct - total_h / 2)

    for line_i, line in enumerate(lines):
        # Compute line width
        line_w = sum(word_sizes[i][0] + style.padding_x * 2 for i in line)
        line_w += gap * max(0, len(line) - 1)
        x = (canvas_width - line_w) // 2
        y = baseline_y + line_i * line_height

        for i in line:
            ww, wh = word_sizes[i]
            is_active = i == active_index
            box_w = ww + style.padding_x * 2
            box_h = wh + style.padding_y * 2

            if is_active and style.active_bg[3] > 0:
                # Rounded gold box
                radius = min(16, box_h // 3)
                draw.rounded_rectangle(
                    [(x, y), (x + box_w, y + box_h)],
                    radius=radius,
                    fill=style.active_bg,
                )

            text_color = style.active_text if is_active else style.text_color
            text_x = x + style.padding_x
            text_y = y + style.padding_y

            # Stroke (outline) — skip for active word with background fill
            if style.stroke_width > 0 and not (is_active and style.active_bg[3] > 0):
                draw.text(
                    (text_x, text_y),
                    page[i].text,
                    font=font,
                    fill=text_color,
                    stroke_width=style.stroke_width,
                    stroke_fill=style.stroke_color,
                )
            else:
                draw.text((text_x, text_y), page[i].text, font=font, fill=text_color)

            x += box_w + gap

    return img


@dataclass
class _CaptionFrame:
    """One rendered caption state with its time range."""
    png_path: Path
    start_s: float
    end_s: float


def render_caption_frames(
    words: list[Word],
    style: CaptionStyle,
    canvas_width: int,
    canvas_height: int,
    output_dir: Path,
) -> list[_CaptionFrame]:
    """Render all unique caption states as PNGs. One per (page × active_word)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    pages = _build_pages(words, style.words_per_page)
    frames: list[_CaptionFrame] = []
    for page_i, page in enumerate(pages):
        if not page:
            continue
        for word_i, word in enumerate(page):
            img = _render_page_png(page, word_i, style, canvas_width, canvas_height)
            png_path = output_dir / f"caption_{page_i:03d}_{word_i:02d}.png"
            img.save(png_path, "PNG")
            frames.append(_CaptionFrame(
                png_path=png_path,
                start_s=word.start_s,
                end_s=word.end_s,
            ))
    return frames


def _build_overlay_filter(frames: list[_CaptionFrame], base_inputs: int = 1) -> tuple[str, list[str]]:
    """Build ffmpeg filter_complex for overlaying all caption frames.

    Returns (filter_string, extra_input_args).
    """
    if not frames:
        return "", []

    # Each frame is a separate input. Chain overlays: [0:v][1:v]overlay...[v1];[v1][2:v]overlay...[v2];...
    inputs: list[str] = []
    for f in frames:
        inputs.extend(["-i", str(f.png_path)])

    filter_parts: list[str] = []
    current_label = "[0:v]"
    for i, f in enumerate(frames):
        input_label = f"[{i + base_inputs}:v]"
        out_label = f"[v{i + 1}]"
        enable_expr = f"between(t,{f.start_s:.3f},{f.end_s:.3f})"
        filter_parts.append(
            f"{current_label}{input_label}overlay=0:0:enable='{enable_expr}'{out_label}"
        )
        current_label = out_label

    filter_string = ";".join(filter_parts)
    return filter_string, inputs


async def burn_captions(
    clip_path: Path,
    words: list[Word],
    output_path: Path,
    *,
    style_name: str = "captions_ai",
    canvas_width: int = 1080,
    canvas_height: int = 1920,
    work_dir: Path | None = None,
) -> Path:
    """Burn word-by-word captions onto a clip.

    Args:
        clip_path: Input MP4 (already reframed).
        words: Clip-local words (use `clip_local_words` to translate from source).
        output_path: Output MP4 with captions burned in.
        style_name: Key in STYLES dict.
    """
    from engine.source import _run

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not installed")

    style = STYLES.get(style_name)
    if style is None:
        raise ValueError(f"Unknown caption style: {style_name}. Available: {list(STYLES)}")

    if not words:
        logger.warning("No words in clip range — copying source unchanged")
        shutil.copy(clip_path, output_path)
        return output_path

    work_dir = work_dir or clip_path.parent / f".captions_{clip_path.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)

    frames = render_caption_frames(words, style, canvas_width, canvas_height, work_dir)
    if not frames:
        shutil.copy(clip_path, output_path)
        return output_path

    filter_complex, extra_inputs = _build_overlay_filter(frames)
    final_label = f"[v{len(frames)}]"

    cmd: list[str] = ["ffmpeg", "-y", "-i", str(clip_path)]
    cmd.extend(extra_inputs)
    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", final_label,
    ])
    # Map audio from source
    cmd.extend(["-map", "0:a?", "-c:a", "copy"])
    cmd.extend([
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ])

    logger.info("Burning %d caption frames onto %s", len(frames), clip_path.name)
    rc, out, err = await _run(*cmd, timeout=max(600, len(frames) * 2))
    if rc != 0:
        raise RuntimeError(f"Caption burn failed: {err[-1000:]}")

    # Cleanup work dir
    try:
        shutil.rmtree(work_dir)
    except OSError:
        pass

    return output_path
