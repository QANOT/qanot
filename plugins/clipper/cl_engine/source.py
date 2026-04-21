"""Video source loader — yt-dlp for URLs, ffprobe for local files, Telegram files."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from cl_engine.models import SourceMedia

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


async def _probe_bgutil_provider(base_url: str = "http://127.0.0.1:4416") -> bool:
    """Check if the bgutil PO-token HTTP provider sidecar is reachable."""
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as s:
            async with s.get(f"{base_url}/ping") as resp:
                return resp.status == 200
    except Exception:
        return False


def _find_cookies_file(output_dir: Path) -> Path | None:
    """Find a YouTube cookies.txt in known locations.

    Priority: clipper/cookies.txt > uploads/cookies.txt > workspace/cookies.txt
    """
    workspace_root = output_dir.parent.parent  # output_dir = workspace/clipper/sources
    candidate_paths = [
        output_dir.parent / "cookies.txt",
        workspace_root / "uploads" / "cookies.txt",
        workspace_root / "cookies.txt",
    ]
    for p in candidate_paths:
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def _base_ytdlp_args() -> list[str]:
    """Args used for every yt-dlp call."""
    return [
        "--no-warnings",
        "--no-playlist",
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    ]


async def _try_probe(url: str, extra_args: list[str], *, timeout: float = 60.0) -> tuple[int, dict | None, str]:
    """Run yt-dlp --dump-json with the given args. Returns (rc, meta_or_None, stderr)."""
    rc, out, err = await _run(
        "yt-dlp", *_base_ytdlp_args(), *extra_args,
        "--dump-json", "--skip-download", url,
        timeout=timeout,
    )
    if rc != 0:
        return rc, None, err
    try:
        return rc, json.loads(out.splitlines()[0]), err
    except (json.JSONDecodeError, IndexError):
        return rc, None, err


def _is_bot_check_error(stderr: str) -> bool:
    s = stderr.lower()
    return any(k in s for k in ("sign in to confirm", "confirm you", "not a bot", "403"))


async def download_url(url: str, output_dir: Path, max_height: int = 1080) -> SourceMedia:
    """Download a video from URL via yt-dlp with production-grade fallback chain.

    Strategy (YouTube-specific — other hosts work on first try):
      1. Try with bgutil PO-token provider (sidecar on :4416)
      2. Try with cookies.txt (if user uploaded one)
      3. Try Android client (works for some videos)
      4. Raise with clear instruction
    """
    if shutil.which("yt-dlp") is None:
        raise RuntimeError("yt-dlp not installed — pip install yt-dlp")

    output_dir.mkdir(parents=True, exist_ok=True)
    template = str(output_dir / "%(id)s.%(ext)s")

    cookies_file = _find_cookies_file(output_dir)
    pot_available = await _probe_bgutil_provider()
    is_youtube = any(h in url.lower() for h in ("youtube.com", "youtu.be"))

    # Build candidate strategies in priority order
    strategies: list[tuple[str, list[str]]] = []

    if is_youtube and pot_available:
        strategies.append((
            "bgutil-pot",
            # Default client with PO token — most reliable for YouTube in 2026
            ["--extractor-args", "youtube:player_client=default,-tv_simply"],
        ))

    if cookies_file is not None:
        strategies.append((
            f"cookies ({cookies_file.name})",
            ["--cookies", str(cookies_file)],
        ))

    if is_youtube:
        strategies.append((
            "android-client",
            ["--extractor-args", "youtube:player_client=android,web_embedded"],
        ))

    # Non-YouTube fallback: plain yt-dlp
    if not strategies:
        strategies.append(("default", []))

    # Try each strategy for the probe
    meta: dict | None = None
    last_err: str = ""
    chosen_strategy: str = ""
    for name, extra_args in strategies:
        logger.info("yt-dlp probe: trying strategy '%s'", name)
        rc, meta_candidate, err = await _try_probe(url, extra_args)
        if meta_candidate is not None:
            meta = meta_candidate
            chosen_strategy = name
            logger.info("yt-dlp probe: strategy '%s' succeeded", name)
            break
        last_err = err
        logger.warning("yt-dlp probe: strategy '%s' failed: %s", name, err[:200])

    if meta is None:
        # All strategies failed — build actionable error
        hint_parts: list[str] = []
        if is_youtube and _is_bot_check_error(last_err):
            if not pot_available:
                hint_parts.append(
                    "The PO-token provider sidecar (bgutil-pot on port 4416) is not running. "
                    "Start it: docker run -d --name bgutil-pot --restart unless-stopped --network host brainicism/bgutil-ytdlp-pot-provider"
                )
            if cookies_file is None:
                hint_parts.append(
                    "Or upload a YouTube cookies.txt via Telegram (save as cookies.txt — "
                    "export with: yt-dlp --cookies-from-browser chrome --cookies cookies.txt)"
                )
            hint_parts.append(
                "Or send the video file directly to the bot as an MP4 — works without any setup."
            )
        hint = ("\n" + "\n".join(f" • {h}" for h in hint_parts)) if hint_parts else ""
        raise RuntimeError(f"yt-dlp couldn't fetch {url}: {last_err[:300]}{hint}")

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

    # Rebuild extra_args for the winning strategy (same params used for probe)
    winning_extra_args: list[str] = []
    for name, extra_args in strategies:
        if name == chosen_strategy:
            winning_extra_args = extra_args
            break

    format_selector = (
        f"bestvideo[ext=mp4][height<={max_height}]+bestaudio[ext=m4a]/"
        f"best[ext=mp4][height<={max_height}]/"
        f"bestvideo[height<={max_height}]+bestaudio/best"
    )

    logger.info("Downloading %s via '%s' (%.0fs, max %dp)...", url, chosen_strategy, duration, max_height)
    rc, out, err = await _run(
        "yt-dlp",
        *_base_ytdlp_args(),
        *winning_extra_args,
        "-f", format_selector,
        "--merge-output-format", "mp4",
        "-o", template,
        url,
        timeout=1800.0,
    )
    if rc != 0:
        raise RuntimeError(f"yt-dlp download failed (strategy={chosen_strategy}): {err[:500]}")

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
