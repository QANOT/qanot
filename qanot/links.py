"""Auto link understanding — fetch and summarize URLs found in user messages."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TypedDict

import aiohttp

from qanot.tools.web import (
    FETCH_USER_AGENT,
    _cache_get,
    _cache_set,
    _extract_html,
    _validate_url,
)

logger = logging.getLogger(__name__)

# Timeout per individual URL fetch (keep low to avoid blocking conversation)
LINK_FETCH_TIMEOUT = 10  # seconds

# Max body size for link previews (smaller than full web_fetch)
LINK_MAX_BODY = 512 * 1024  # 512KB

# Max URLs to process per message
MAX_URLS_PER_MESSAGE = 3

# Max total characters for all link previews combined
MAX_TOTAL_PREVIEW_CHARS = 4000

# Default max characters per individual preview
DEFAULT_PREVIEW_CHARS = 2000

# URL patterns to skip (binary files, media, archives)
_SKIP_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp",
    ".mp4", ".mp3", ".avi", ".mov", ".webm", ".mkv", ".flac", ".wav",
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z", ".dmg", ".iso",
    ".exe", ".msi", ".deb", ".rpm", ".apk", ".woff", ".woff2", ".ttf",
})

# URL patterns to skip (non-content endpoints)
_SKIP_URL_PATTERNS = re.compile(
    r"(?:"
    r"t\.me/"                     # Telegram links
    r"|api\."                     # API endpoints
    r"|localhost"                 # Local addresses
    r"|127\.0\.0\.1"             # Loopback
    r"|/api/v\d"                 # REST API versioned paths
    r"|/webhook"                 # Webhook endpoints
    r"|\.s3\.amazonaws\.com/"    # S3 direct file links
    r")",
    re.IGNORECASE,
)

# URL extraction regex — matches http/https URLs in text
_URL_PATTERN = re.compile(
    r"https?://"                     # scheme
    r"[^\s<>\"\'\)\]\}]+"            # everything until whitespace or closing chars
)


class LinkPreview(TypedDict):
    url: str
    title: str
    preview: str


# Max URL length to prevent abuse via extremely long URLs
_MAX_URL_LENGTH = 2048

# Pattern to detect control characters, null bytes, and other dangerous chars in URLs
_DANGEROUS_URL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def extract_urls(text: str) -> list[str]:
    """Extract HTTP/HTTPS URLs from text.

    Returns deduplicated list preserving first-occurrence order.
    URLs with control characters, null bytes, or excessive length are rejected.
    """
    seen: set[str] = set()
    urls: list[str] = []

    for match in _URL_PATTERN.finditer(text):
        url = match.group(0)
        # Strip trailing punctuation that's likely not part of the URL
        url = url.rstrip(".,;:!?)>]}\"'")

        # Reject URLs that are too long (potential abuse / buffer attacks)
        if len(url) > _MAX_URL_LENGTH:
            logger.debug("Skipping excessively long URL (%d chars)", len(url))
            continue

        # Reject URLs containing control characters or null bytes
        if _DANGEROUS_URL_CHARS.search(url):
            logger.debug("Skipping URL with dangerous characters: %r", url[:100])
            continue

        # Reject URLs with embedded credentials (user:pass@host)
        if "@" in url.split("//", 1)[-1].split("/", 1)[0]:
            logger.debug("Skipping URL with embedded credentials")
            continue

        if url not in seen:
            seen.add(url)
            urls.append(url)

    return urls


def _should_skip_url(url: str) -> bool:
    """Check if a URL should be skipped (binary, media, non-content)."""
    url_lower = url.lower()

    # Check file extensions
    # Strip query string/fragment before checking extension
    path_part = url_lower.split("?")[0].split("#")[0]
    # Find the last dot to extract the extension for O(1) set lookup
    dot_idx = path_part.rfind(".")
    if dot_idx != -1 and path_part[dot_idx:] in _SKIP_EXTENSIONS:
        return True

    # Check URL patterns
    if _SKIP_URL_PATTERNS.search(url_lower):
        return True

    return False


async def fetch_url_preview(
    url: str,
    max_chars: int = DEFAULT_PREVIEW_CHARS,
) -> LinkPreview | None:
    """Fetch a URL and extract title + text preview.

    Returns a LinkPreview dict or None on any error.
    Reuses SSRF validation and caching from web.py.
    """
    # Check cache first
    cache_key = f"link_preview:{url}"
    cached = _cache_get(cache_key)
    if cached is not None:
        # cached is a string — we stored "title\x00preview"
        parts = cached.split("\x00", 1)
        if len(parts) == 2:
            return LinkPreview(url=url, title=parts[0], preview=parts[1])

    # SSRF validation
    ssrf_error = _validate_url(url)
    if ssrf_error:
        logger.debug("Link preview SSRF blocked: %s — %s", url, ssrf_error)
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=LINK_FETCH_TIMEOUT)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": FETCH_USER_AGENT},
        ) as session:
            async with session.get(
                url,
                max_redirects=3,
                allow_redirects=True,
            ) as resp:
                # Validate final URL after redirects
                final_url = str(resp.url)
                if final_url != url:
                    redirect_error = _validate_url(final_url)
                    if redirect_error:
                        logger.debug("Link preview redirect SSRF blocked: %s", final_url)
                        return None

                content_type = resp.content_type or ""
                if "html" not in content_type and "text" not in content_type:
                    # Not a text-based page — skip silently
                    return None

                # Read body with size limit
                chunks: list[bytes] = []
                total_read = 0
                async for chunk in resp.content.iter_chunked(8192):
                    chunks.append(chunk)
                    total_read += len(chunk)
                    if total_read > LINK_MAX_BODY:
                        break
                body_bytes = b"".join(chunks)

                charset = resp.charset or "utf-8"
                try:
                    body = body_bytes.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    body = body_bytes.decode("utf-8", errors="replace")

        # Extract content
        if "html" in content_type:
            text, title = _extract_html(body)
        else:
            text = body
            title = ""

        # Truncate preview
        preview = text[:max_chars].strip()
        if len(text) > max_chars:
            # Try to break at last sentence or paragraph boundary
            for boundary in ("\n\n", "\n", ". ", "? ", "! "):
                last_break = preview.rfind(boundary)
                if last_break > max_chars // 2:
                    preview = preview[: last_break + len(boundary)].strip()
                    break

        if not preview:
            return None

        result = LinkPreview(url=url, title=title, preview=preview)

        # Cache the result
        _cache_set(cache_key, f"{title}\x00{preview}")
        logger.info("Link preview fetched: %s (%s)", url, title or "no title")

        return result

    except asyncio.TimeoutError:
        logger.debug("Link preview timeout: %s", url)
        return None
    except aiohttp.ClientError as exc:
        logger.debug("Link preview fetch error for %s: %s", url, exc)
        return None
    except Exception as exc:
        logger.debug("Link preview unexpected error for %s: %s", url, exc)
        return None


async def fetch_link_previews(
    text: str,
    max_urls: int = MAX_URLS_PER_MESSAGE,
    max_total_chars: int = MAX_TOTAL_PREVIEW_CHARS,
) -> str:
    """Extract URLs from text, fetch previews, return formatted context string.

    Returns a formatted block suitable for injection into conversation context,
    or an empty string if no URLs were found or all fetches failed.
    """
    urls = extract_urls(text)
    if not urls:
        return ""

    # Filter out non-content URLs
    urls = [u for u in urls if not _should_skip_url(u)]
    if not urls:
        return ""

    # Limit to max_urls
    urls = urls[:max_urls]

    # Fetch all URLs concurrently
    per_url_chars = max_total_chars // len(urls)
    tasks = [fetch_url_preview(url, max_chars=per_url_chars) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect successful previews
    previews: list[LinkPreview] = []
    total_chars = 0
    for result in results:
        if isinstance(result, Exception) or result is None:
            continue
        preview_len = len(result["preview"]) + len(result["title"]) + len(result["url"])
        if total_chars + preview_len > max_total_chars:
            # Truncate this preview to fit
            remaining = max_total_chars - total_chars
            if remaining < 200:
                break  # Not enough space for a meaningful preview
            result = LinkPreview(
                url=result["url"],
                title=result["title"],
                preview=result["preview"][:remaining],
            )
        previews.append(result)
        total_chars += len(result["preview"]) + len(result["title"]) + len(result["url"])

    if not previews:
        return ""

    # Format output
    sections: list[str] = []
    for p in previews:
        section = f"**{p['url']}**"
        if p["title"]:
            section += f"\nTitle: {p['title']}"
        section += f"\nPreview: {p['preview']}"
        sections.append(section)

    body = "\n\n".join(sections)
    return (
        "[LINK CONTEXT — auto-fetched from URLs in your message]\n\n"
        f"{body}"
    )
