"""Video source loader — yt-dlp for URLs, ffprobe for local files, Telegram files."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from engine.models import SourceMedia

logger = logging.getLogger(__name__)

# Max safe source duration (hours). Guards against downloading a 10-hour stream.
MAX_SOURCE_DURATION_S = 4 * 3600  # 4 hours
MAX_SOURCE_SIZE_MB = 2000  # 2 GB

_URL_SCHEMES = {"http", "https"}


def is_url(source: str) -> bool:
    """Return True if source looks like a URL."""
    try:
        parsed = urlparse(source)
        return parsed.scheme.lower() in _URL_SCHEMES and bool(parsed.netloc)
    except Exception:
        return False


async def _run(*cmd: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def probe_media(path: Path) -> dict:
    """Run ffprobe and return stream info as dict."""
    rc, out, err = await _run(
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(path),
        timeout=30.0,
    )
    if rc != 0:
        raise RuntimeError(f"ffprobe failed: {err}")
    return json.loads(out)


def _parse_fps(rate_str: str) -> float:
    """Parse ffprobe rate string like '30000/1001'."""
    if "/" in rate_str:
        num, den = rate_str.split("/", 1)
        try:
            n, d = float(num), float(den)
            return n / d if d else 0.0
        except ValueError:
            return 0.0
    try:
        return float(rate_str)
    except ValueError:
        return 0.0


async def probe_to_source(path: Path, original_url: str | None = None, title: str | None = None) -> SourceMedia:
    """Probe a media file and return SourceMedia."""
    info = await probe_media(path)
    video_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video_stream is None:
        raise ValueError(f"No video stream in {path}")

    duration_s = float(info.get("format", {}).get("duration", 0.0))
    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    fps = _parse_fps(video_stream.get("r_frame_rate", "0/1"))
    has_audio = audio_stream is not None

    if duration_s > MAX_SOURCE_DURATION_S:
        raise ValueError(
            f"Source too long: {duration_s:.0f}s > {MAX_SOURCE_DURATION_S}s limit"
        )

    return SourceMedia(
        path=path,
        duration_s=duration_s,
        width=width,
        height=height,
        fps=fps,
        has_audio=has_audio,
        original_url=original_url,
        title=title,
    )


async def download_url(url: str, output_dir: Path, max_height: int = 1080) -> SourceMedia:
    """Download a video from URL via yt-dlp.

    Uses best mp4/m4a streams up to max_height. Writes to output_dir.
    Returns SourceMedia with local path.
    """
    if shutil.which("yt-dlp") is None:
        raise RuntimeError("yt-dlp not installed — pip install yt-dlp")

    output_dir.mkdir(parents=True, exist_ok=True)
    # Use deterministic filename pattern so we can find the output
    template = str(output_dir / "%(id)s.%(ext)s")

    # First probe via --dump-json to get metadata without downloading
    rc, out, err = await _run(
        "yt-dlp",
        "--no-warnings",
        "--dump-json",
        "--no-playlist",
        "--skip-download",
        url,
        timeout=60.0,
    )
    if rc != 0:
        raise RuntimeError(f"yt-dlp metadata probe failed: {err[:500]}")

    try:
        meta = json.loads(out.splitlines()[0])
    except (json.JSONDecodeError, IndexError) as e:
        raise RuntimeError(f"Failed to parse yt-dlp metadata: {e}")

    duration = float(meta.get("duration") or 0)
    if duration > MAX_SOURCE_DURATION_S:
        raise ValueError(f"Source too long: {duration:.0f}s > {MAX_SOURCE_DURATION_S}s limit")

    filesize = meta.get("filesize_approx") or meta.get("filesize") or 0
    if filesize and filesize > MAX_SOURCE_SIZE_MB * 1024 * 1024:
        raise ValueError(f"Source too large: {filesize / 1_048_576:.0f}MB > {MAX_SOURCE_SIZE_MB}MB limit")

    video_id = meta.get("id", "source")
    title = meta.get("title")

    # Check if already downloaded (idempotent)
    for existing in output_dir.glob(f"{video_id}.*"):
        if existing.suffix in {".mp4", ".mkv", ".webm"}:
            logger.info("Reusing cached download: %s", existing)
            return await probe_to_source(existing, original_url=url, title=title)

    # Download best mp4 up to max_height (prefer mp4 for ffmpeg compatibility)
    format_selector = (
        f"bestvideo[ext=mp4][height<={max_height}]+bestaudio[ext=m4a]/"
        f"best[ext=mp4][height<={max_height}]/"
        f"bestvideo[height<={max_height}]+bestaudio/best"
    )

    logger.info("Downloading %s (%.0fs, max %dp)...", url, duration, max_height)
    rc, out, err = await _run(
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "-f", format_selector,
        "--merge-output-format", "mp4",
        "-o", template,
        url,
        timeout=1800.0,  # 30 min download budget
    )
    if rc != 0:
        raise RuntimeError(f"yt-dlp download failed: {err[:500]}")

    # Find the downloaded file
    candidates = sorted(output_dir.glob(f"{video_id}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    video_file = next((p for p in candidates if p.suffix in {".mp4", ".mkv", ".webm"}), None)
    if video_file is None:
        raise RuntimeError(f"yt-dlp did not produce a video file in {output_dir}")

    return await probe_to_source(video_file, original_url=url, title=title)


async def load_local(path: Path) -> SourceMedia:
    """Load a local video file."""
    if not path.exists():
        raise FileNotFoundError(f"Source not found: {path}")
    size_mb = path.stat().st_size / 1_048_576
    if size_mb > MAX_SOURCE_SIZE_MB:
        raise ValueError(f"Source too large: {size_mb:.0f}MB > {MAX_SOURCE_SIZE_MB}MB limit")
    return await probe_to_source(path)


async def load_source(source: str | Path, output_dir: Path, max_height: int = 1080) -> SourceMedia:
    """Load a source from URL or local path.

    Args:
        source: URL (http/https), local path string, or Path.
        output_dir: Where to download URL sources.
        max_height: Max video height for downloads (cost/quality tradeoff).
    """
    if isinstance(source, Path):
        return await load_local(source)
    s = str(source).strip()
    if is_url(s):
        return await download_url(s, output_dir, max_height=max_height)
    return await load_local(Path(s))


async def extract_audio(source: SourceMedia, output_path: Path, sample_rate: int = 16000) -> Path:
    """Extract mono WAV at sample_rate for transcription."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rc, out, err = await _run(
        "ffmpeg",
        "-y",
        "-i", str(source.path),
        "-vn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        str(output_path),
        timeout=600.0,
    )
    if rc != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed: {err[:500]}")
    return output_path
