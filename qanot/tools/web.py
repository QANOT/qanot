"""Web tools — Brave Search API + web_fetch with SSRF protection."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import aiohttp

from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Brave Search API
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_TIMEOUT = 15  # seconds

# web_fetch constants
FETCH_TIMEOUT = 30  # seconds
FETCH_MAX_BODY = 2 * 1024 * 1024  # 2MB
FETCH_MAX_REDIRECTS = 3
FETCH_DEFAULT_MAX_CHARS = 50_000
FETCH_MAX_BODY_MB = FETCH_MAX_BODY // (1024 * 1024)
FETCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Blocked hostnames for SSRF protection
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "metadata.google.internal",
    "metadata.google.internal.",
})

# Blocked ports for SSRF protection (common internal services)
_BLOCKED_PORTS = frozenset({
    22,    # SSH
    25,    # SMTP
    110,   # POP3
    143,   # IMAP
    3306,  # MySQL
    5432,  # PostgreSQL
    6379,  # Redis
    27017, # MongoDB
    9200,  # Elasticsearch
    2375,  # Docker daemon
})

# Private/reserved IP networks to block
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
]

# In-memory cache (thread-safe via lock)
CACHE_TTL = 900  # 15 minutes
CACHE_MAX = 50


class _ThreadSafeCache:
    """Simple TTL cache with lock protection for concurrent async access."""

    __slots__ = ("_data", "_lock", "_ttl", "_max")

    def __init__(self, ttl: int = CACHE_TTL, max_size: int = CACHE_MAX):
        self._data: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()
        self._ttl = ttl
        self._max = max_size

    async def get(self, key: str) -> str | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, result = entry
            if time.monotonic() - ts > self._ttl:
                self._data.pop(key, None)
                return None
            return result

    async def set(self, key: str, result: str) -> None:
        async with self._lock:
            if len(self._data) >= self._max:
                oldest_key = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(oldest_key, None)
            self._data[key] = (time.monotonic(), result)

    def clear(self) -> None:
        """Clear all cache entries (for testing)."""
        self._data.clear()


_cache = _ThreadSafeCache()


# ── SSRF protection ──────────────────────────────────────────────

def _is_ip_blocked(ip_str: str) -> bool:
    """Check if an IP address belongs to a private/reserved network."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # Unparseable = blocked
    return any(addr in net for net in _BLOCKED_NETWORKS)


def _validate_url(url: str) -> str | None:
    """Validate URL for SSRF safety. Returns error message or None if safe."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL"

    if parsed.scheme not in ("http", "https"):
        return "Invalid URL scheme — only http:// and https:// allowed"

    hostname = parsed.hostname
    if not hostname:
        return "Invalid URL — no hostname"

    if hostname.lower() in _BLOCKED_HOSTNAMES:
        return "URL blocked: private/internal network address"

    # Block common internal service ports
    port = parsed.port
    if port is not None and port in _BLOCKED_PORTS:
        return f"URL blocked: port {port} is not allowed"

    # DNS resolution to check actual IP
    try:
        addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return f"DNS resolution failed for {hostname}"

    for family, _, _, _, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        if _is_ip_blocked(ip_str):
            return "URL blocked: private/internal network address"

    return None


# ── HTML text extraction ─────────────────────────────────────────

class _ReadabilityExtractor(HTMLParser):
    """Extract readable text from HTML, converting to simplified markdown.

    Strips script/style/nav/footer/header content. Converts headings to
    markdown, links to [text](url) format, preserves paragraph breaks.
    """

    _SKIP_TAGS = frozenset({"script", "style", "nav", "footer", "header", "noscript", "svg"})
    _HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
    _BLOCK_TAGS = frozenset({
        "p", "div", "section", "article", "main", "blockquote",
        "li", "tr", "br", "hr",
    })

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth: int = 0
        self._tag_stack: list[str] = []
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._in_link: bool = False
        self.title: str = ""
        self._in_title: bool = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self._tag_stack.append(tag)

        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return

        if self._skip_depth > 0:
            return

        if tag == "title":
            self._in_title = True
            self._title_parts = []
            return

        if tag in self._HEADING_TAGS:
            level = int(tag[1])
            self._chunks.append("\n\n" + "#" * level + " ")
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n\n")
        elif tag == "br":
            self._chunks.append("\n")
        elif tag == "a":
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href", "")
            if href and not href.startswith(("#", "javascript:")):
                self._in_link = True
                self._link_href = href
                self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

        if tag == "title":
            self._in_title = False
            self.title = " ".join(self._title_parts).strip()

        if tag == "a" and self._in_link:
            self._in_link = False
            text = "".join(self._link_text).strip()
            if text and self._link_href:
                self._chunks.append(f"[{text}]({self._link_href})")
            elif text:
                self._chunks.append(text)
            self._link_href = None
            self._link_text = []

        if tag in self._HEADING_TAGS:
            self._chunks.append("\n")

        # Pop tag stack (tolerant of mismatched tags)
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)

        if self._skip_depth > 0:
            return

        if self._in_link:
            self._link_text.append(data)
        else:
            self._chunks.append(data)

    def get_text(self) -> str:
        """Return extracted text with normalized whitespace."""
        raw = "".join(self._chunks)
        # Collapse multiple blank lines to max two newlines
        text = re.sub(r"\n{3,}", "\n\n", raw)
        # Collapse multiple spaces on same line
        text = re.sub(r"[^\S\n]+", " ", text)
        # Strip leading/trailing whitespace per line
        lines = [line.strip() for line in text.splitlines()]
        return "\n".join(lines).strip()


def _extract_html(html: str) -> tuple[str, str]:
    """Extract readable text and title from HTML.

    Returns (text_content, title).
    """
    extractor = _ReadabilityExtractor()
    try:
        extractor.feed(html)
    except Exception:
        # Fallback: strip all tags
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text, ""
    return extractor.get_text(), extractor.title


async def _cache_get(key: str) -> str | None:
    """Get cached result if not expired (async, lock-protected)."""
    return await _cache.get(key)


async def _cache_set(key: str, result: str) -> None:
    """Cache a result, evicting oldest if over limit (async, lock-protected)."""
    await _cache.set(key, result)


def _format_results(data: dict, query: str) -> str:
    """Format Brave API response into clean text for the LLM."""
    web = data.get("web", {})
    results = web.get("results", [])

    if not results:
        return json.dumps({
            "query": query,
            "results": [],
            "message": "No results found.",
        })

    formatted = []
    for r in results:
        entry: dict[str, Any] = {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "description": r.get("description", "").replace("<strong>", "").replace("</strong>", ""),
        }
        if age := r.get("age"):
            entry["age"] = age
        formatted.append(entry)

    return json.dumps({
        "query": query,
        "source": "[web search — external content, may be inaccurate]",
        "count": len(formatted),
        "results": formatted,
    }, ensure_ascii=False)


def register_web_tools(
    registry: ToolRegistry,
    brave_api_key: str,
) -> None:
    """Register web search tools."""

    async def web_search(params: dict) -> str:
        """Search the web using Brave Search API."""
        query = params.get("query", "").strip()
        if not query:
            return json.dumps({"error": "Query is required"})
        if len(query) > 2000:
            return json.dumps({"error": "Query too long (max 2000 characters)"})

        try:
            count = int(params.get("count", 5))
        except (TypeError, ValueError):
            return json.dumps({"error": "count must be an integer"})
        count = max(1, min(count, 10))

        # Check cache
        cache_key = f"{query.lower()}:{count}"
        cached = await _cache_get(cache_key)
        if cached:
            logger.debug("Web search cache hit: %s", query)
            return cached

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Accept": "application/json",
                    "X-Subscription-Token": brave_api_key,
                }
                api_params = {
                    "q": query,
                    "count": str(count),
                }
                async with session.get(
                    BRAVE_API_URL,
                    headers=headers,
                    params=api_params,
                    timeout=aiohttp.ClientTimeout(total=BRAVE_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error("Brave API error %d: %s", resp.status, error_text[:200])
                        return json.dumps({"error": f"Search API error ({resp.status})"})

                    data = await resp.json()
                    result = _format_results(data, query)
                    await _cache_set(cache_key, result)
                    return result

        except aiohttp.ClientError as e:
            logger.error("Web search network error: %s", e)
            return json.dumps({"error": "Search request failed. Try again."})
        except Exception as e:
            logger.error("Web search error: %s", e)
            return json.dumps({"error": str(e)})

    registry.register(
        name="web_search",
        description=(
            "Search the web for current information. Use this for: real-time data "
            "(weather, news, prices, events), facts you're unsure about, "
            "anything that may have changed after your training data."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — be specific and concise",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results (1-10, default 5)",
                },
            },
            "required": ["query"],
        },
        handler=web_search,
        category="web",
    )

    # ── web_fetch ──────────────────────────────────────────────────

    async def web_fetch(params: dict) -> str:
        """Fetch and extract readable content from a web page URL."""
        url = params.get("url", "").strip()
        if not url:
            return json.dumps({"error": "url is required"})

        try:
            max_chars = int(params.get("max_chars", FETCH_DEFAULT_MAX_CHARS))
        except (TypeError, ValueError):
            max_chars = FETCH_DEFAULT_MAX_CHARS

        # Validate URL scheme
        ssrf_error = await asyncio.to_thread(_validate_url, url)
        if ssrf_error:
            return json.dumps({"error": ssrf_error})

        # Check cache
        cache_key = f"fetch:{url}:{max_chars}"
        cached = await _cache_get(cache_key)
        if cached:
            logger.debug("web_fetch cache hit: %s", url)
            return cached

        try:
            timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": FETCH_USER_AGENT},
            ) as session:
                async with session.get(
                    url,
                    max_redirects=FETCH_MAX_REDIRECTS,
                    allow_redirects=True,
                ) as resp:
                    # Check final URL after redirects for SSRF
                    final_url = str(resp.url)
                    if final_url != url:
                        redirect_error = await asyncio.to_thread(_validate_url, final_url)
                        if redirect_error:
                            return json.dumps({"error": redirect_error})

                    # Check content length before reading
                    content_length = resp.content_length
                    if content_length is not None and content_length > FETCH_MAX_BODY:
                        return json.dumps({
                            "error": f"Response too large (>{FETCH_MAX_BODY_MB}MB)",
                        })

                    # Read body with size limit
                    body_bytes = b""
                    async for chunk in resp.content.iter_chunked(8192):
                        body_bytes += chunk
                        if len(body_bytes) > FETCH_MAX_BODY:
                            return json.dumps({
                                "error": f"Response too large (>{FETCH_MAX_BODY_MB}MB)",
                            })

                    content_type = resp.content_type or ""
                    charset = resp.charset or "utf-8"

                    try:
                        body = body_bytes.decode(charset, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        body = body_bytes.decode("utf-8", errors="replace")

            # Extract content based on type
            title = ""
            if "html" in content_type:
                content, title = _extract_html(body)
            elif "json" in content_type:
                try:
                    parsed = json.loads(body)
                    content = json.dumps(parsed, indent=2, ensure_ascii=False)
                except json.JSONDecodeError:
                    content = body
            else:
                # Plain text, markdown, XML, etc.
                content = body

            # Apply output limit
            total_len = len(content)
            truncated = False
            if total_len > max_chars:
                content = content[:max_chars]
                truncated = True

            if truncated:
                content += f"\n\n[... truncated, {total_len} total chars]"

            result = json.dumps({
                "url": url,
                "final_url": final_url,
                "title": title,
                "content": content,
                "content_type": content_type,
                "length": total_len,
                "source": "[web content — external, may be inaccurate]",
            }, ensure_ascii=False)

            await _cache_set(cache_key, result)
            return result

        except aiohttp.TooManyRedirects:
            return json.dumps({"error": f"Too many redirects (max {FETCH_MAX_REDIRECTS})"})
        except aiohttp.ClientError as e:
            if "timeout" in str(e).lower():
                return json.dumps({"error": f"Request timed out ({FETCH_TIMEOUT}s)"})
            logger.error("web_fetch network error for %s: %s", url, e)
            return json.dumps({"error": f"Request failed: {type(e).__name__}"})
        except asyncio.TimeoutError:
            return json.dumps({"error": f"Request timed out ({FETCH_TIMEOUT}s)"})
        except Exception as e:
            logger.error("web_fetch error for %s: %s", url, e)
            return json.dumps({"error": str(e)})

    registry.register(
        name="web_fetch",
        description=(
            "Fetch and read the content of a web page URL. Returns extracted "
            "text content. Use this to read articles, documentation, or any web page."
        ),
        parameters={
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to fetch (http:// or https://)",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Max output characters (default 50000)",
                },
            },
        },
        handler=web_fetch,
        category="web",
    )
