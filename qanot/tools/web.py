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
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.google.internal.",
    "metadata",  # short form sometimes resolved on cloud VMs
})

# Hostname suffixes treated as internal (any *.localhost, *.local, *.internal)
_BLOCKED_HOSTNAME_SUFFIXES = (
    ".localhost", ".local", ".internal", ".lan", ".intranet", ".corp", ".home",
)

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

# Private/reserved IP networks to block. is_private/is_loopback/is_link_local
# from the stdlib catches most cases, but we keep an explicit list for the
# ranges where Python's flags are version-dependent or absent.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT (RFC 6598)
    ipaddress.ip_network("0.0.0.0/8"),         # "this network"
    ipaddress.ip_network("224.0.0.0/4"),       # multicast
    ipaddress.ip_network("240.0.0.0/4"),       # reserved class E
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
    ipaddress.ip_network("fc00::/7"),          # IPv6 ULA (RFC 4193)
    ipaddress.ip_network("::ffff:0:0/96"),     # IPv4-mapped IPv6
    ipaddress.ip_network("64:ff9b::/96"),      # NAT64 well-known prefix
    ipaddress.ip_network("2001:db8::/32"),     # IPv6 documentation
    ipaddress.ip_network("ff00::/8"),          # IPv6 multicast
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
    """Check if an IP belongs to a private/reserved/internal network.

    Uses both stdlib flags (is_private, is_loopback, is_link_local,
    is_reserved, is_multicast, is_unspecified) and an explicit
    _BLOCKED_NETWORKS list for ranges Python's flags don't cover (CGNAT
    on older Pythons, IPv6 ULA, IPv4-mapped IPv6, NAT64, doc range).
    Recursively checks the IPv4 inside an IPv4-mapped IPv6 address.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # Unparseable = blocked
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return True
    if addr.is_reserved or addr.is_multicast or addr.is_unspecified:
        return True
    if any(addr in net for net in _BLOCKED_NETWORKS):
        return True
    # IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254): unwrap and re-check.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return _is_ip_blocked(str(addr.ipv4_mapped))
    return False


def _is_blocked_hostname(hostname: str) -> bool:
    """Match hostname against literal blocklist + suffix patterns."""
    h = hostname.lower().rstrip(".")
    if not h:
        return True
    if h in _BLOCKED_HOSTNAMES:
        return True
    for suffix in _BLOCKED_HOSTNAME_SUFFIXES:
        if h.endswith(suffix):
            return True
    return False


def _resolve_safe_addrs(
    hostname: str,
) -> tuple[str | None, list[dict[str, Any]] | None]:
    """Resolve a hostname and validate every returned IP.

    Returns (error, addr_list). On success the addr_list is suitable for
    feeding into _PinnedResolver — every entry has been checked against
    _is_ip_blocked. The connection later happens against these pinned IPs,
    so a low-TTL DNS rebinding can no longer pivot to a private target
    between validation and connect.
    """
    try:
        addr_infos = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror:
        return f"DNS resolution failed for {hostname}", None

    safe: list[dict[str, Any]] = []
    for family, _socktype, proto, _, sockaddr in addr_infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        ip_str = sockaddr[0]
        if _is_ip_blocked(ip_str):
            return "URL blocked: private/internal network address", None
        # Strip IPv6 zone-id if present ("fe80::1%eth0" -> "fe80::1")
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        safe.append(
            {
                "hostname": hostname,
                "host": ip_str,
                "port": 0,
                "family": family,
                "proto": proto,
                "flags": 0,
            }
        )
    if not safe:
        return f"No safe addresses for {hostname}", None
    return None, safe


def _validate_url_metadata(url: str) -> tuple[str | None, str | None]:
    """Check scheme, port, and hostname text without doing DNS.

    Returns (error, hostname). On success error is None.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL", None
    if parsed.scheme not in ("http", "https"):
        return "Invalid URL scheme — only http:// and https:// allowed", None
    hostname = parsed.hostname
    if not hostname:
        return "Invalid URL — no hostname", None
    if _is_blocked_hostname(hostname):
        return "URL blocked: private/internal network address", None
    port = parsed.port
    if port is not None and port in _BLOCKED_PORTS:
        return f"URL blocked: port {port} is not allowed", None
    # Reject literal IP-address URLs that resolve to blocked ranges (a user
    # could also bypass DNS entirely by passing http://169.254.169.254/...).
    try:
        ip_literal = ipaddress.ip_address(hostname)
    except ValueError:
        ip_literal = None
    if ip_literal is not None and _is_ip_blocked(str(ip_literal)):
        return "URL blocked: private/internal network address", None
    return None, hostname


def _validate_url(url: str) -> str | None:
    """Back-compat wrapper. Validate URL + resolve all IPs. Returns error or None."""
    err, hostname = _validate_url_metadata(url)
    if err:
        return err
    # Skip DNS for literal IPs (already checked above)
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return None
    err, _addrs = _resolve_safe_addrs(hostname)
    return err


class _PinnedResolver:
    """aiohttp resolver that returns pre-validated IPs only.

    Bridges the TOCTOU between validate-time DNS lookup and connect-time
    DNS lookup: aiohttp's default resolver re-queries DNS, opening the
    rebinding window. With this resolver, aiohttp can ONLY connect to
    addresses we already validated — no further DNS happens.

    Hostnames not pre-pinned are rejected, so a redirect to a different
    host fails closed at the resolver layer.
    """

    def __init__(self, pin: dict[str, list[dict[str, Any]]]):
        # Lower-case keys so case-variant Host headers still match.
        self._pin = {k.lower(): v for k, v in pin.items()}

    def add(self, hostname: str, addrs: list[dict[str, Any]]) -> None:
        self._pin[hostname.lower()] = addrs

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict[str, Any]]:
        key = host.lower().rstrip(".")
        if key not in self._pin:
            raise OSError(
                f"hostname {host!r} is not pre-pinned for SSRF safety"
            )
        results = []
        for entry in self._pin[key]:
            if family == socket.AF_UNSPEC or entry["family"] == family:
                results.append({**entry, "port": port})
        if not results:
            raise OSError(
                f"no pinned address for {host!r} matching family={family}"
            )
        return results

    async def close(self) -> None:
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


async def _read_capped(resp: aiohttp.ClientResponse) -> bytes | str:
    """Read response body up to FETCH_MAX_BODY. Returns bytes or an error JSON
    string if the cap is exceeded. Checks Content-Length first to fail fast."""
    content_length = resp.content_length
    if content_length is not None and content_length > FETCH_MAX_BODY:
        return json.dumps(
            {"error": f"Response too large (>{FETCH_MAX_BODY_MB}MB)"}
        )
    body = b""
    async for chunk in resp.content.iter_chunked(8192):
        body += chunk
        if len(body) > FETCH_MAX_BODY:
            return json.dumps(
                {"error": f"Response too large (>{FETCH_MAX_BODY_MB}MB)"}
            )
    return body


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
        """Fetch and extract readable content from a web page URL.

        SSRF safety: hostname is resolved once, every returned IP is
        validated against the private/internal/cloud-metadata blocklist,
        and the actual connection happens through a pinned-IP resolver
        that rejects any hostname not pre-validated. This closes the
        DNS-rebinding window between validate-time and connect-time
        lookups.
        """
        url = params.get("url", "").strip()
        if not url:
            return json.dumps({"error": "url is required"})

        try:
            max_chars = int(params.get("max_chars", FETCH_DEFAULT_MAX_CHARS))
        except (TypeError, ValueError):
            max_chars = FETCH_DEFAULT_MAX_CHARS

        # Cache check (keyed on the requested URL)
        cache_key = f"fetch:{url}:{max_chars}"
        cached = await _cache_get(cache_key)
        if cached:
            logger.debug("web_fetch cache hit: %s", url)
            return cached

        # Validate the requested URL and pre-resolve every IP. The
        # connector below will use only these addresses.
        meta_err, hostname = await asyncio.to_thread(_validate_url_metadata, url)
        if meta_err:
            return json.dumps({"error": meta_err})

        # If the hostname is a literal IP, the metadata check already
        # validated it. Otherwise we need DNS-resolved pins.
        try:
            ipaddress.ip_address(hostname)
            is_literal_ip = True
        except ValueError:
            is_literal_ip = False

        resolver: _PinnedResolver | None = None
        if not is_literal_ip:
            dns_err, addrs = await asyncio.to_thread(_resolve_safe_addrs, hostname)
            if dns_err:
                return json.dumps({"error": dns_err})
            resolver = _PinnedResolver({hostname: addrs or []})

        try:
            timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
            connector = aiohttp.TCPConnector(resolver=resolver) if resolver else None
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": FETCH_USER_AGENT},
                connector=connector,
            ) as session:
                # Manually walk redirects so each hop re-validates and
                # re-pins. aiohttp's auto-redirect can't be safely combined
                # with a strict pinned resolver across hostnames.
                current_url = url
                final_url = url
                hops = 0
                while True:
                    async with session.get(
                        current_url, allow_redirects=False
                    ) as resp:
                        if resp.status in (301, 302, 303, 307, 308) and hops < FETCH_MAX_REDIRECTS:
                            location = resp.headers.get("Location", "")
                            if not location:
                                # No Location header — treat as terminal
                                final_url = str(resp.url)
                                content_type_local = resp.content_type or ""
                                charset_local = resp.charset or "utf-8"
                                body_bytes = await _read_capped(resp)
                                if isinstance(body_bytes, str):
                                    return body_bytes  # already an error JSON
                                break
                            from urllib.parse import urljoin
                            next_url = urljoin(current_url, location)
                            err, next_host = await asyncio.to_thread(
                                _validate_url_metadata, next_url
                            )
                            if err:
                                return json.dumps({"error": err})
                            if next_host and resolver is not None:
                                try:
                                    ipaddress.ip_address(next_host)
                                except ValueError:
                                    derr, daddrs = await asyncio.to_thread(
                                        _resolve_safe_addrs, next_host
                                    )
                                    if derr:
                                        return json.dumps({"error": derr})
                                    resolver.add(next_host, daddrs or [])
                            current_url = next_url
                            final_url = next_url
                            hops += 1
                            continue
                        # Terminal response — read body
                        final_url = str(resp.url)
                        content_type_local = resp.content_type or ""
                        charset_local = resp.charset or "utf-8"
                        body_bytes = await _read_capped(resp)
                        if isinstance(body_bytes, str):
                            return body_bytes  # error JSON
                        break

                if hops >= FETCH_MAX_REDIRECTS and current_url != final_url:
                    return json.dumps(
                        {"error": f"Too many redirects (max {FETCH_MAX_REDIRECTS})"}
                    )

                try:
                    body = body_bytes.decode(charset_local, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    body = body_bytes.decode("utf-8", errors="replace")

            content_type = content_type_local
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
                content = body

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
        except OSError as e:
            # Pinned resolver refused (hostname not pre-resolved) or kernel
            # connection error — treat as SSRF block.
            logger.warning("web_fetch resolver/connect rejected %s: %s", url, e)
            return json.dumps({"error": f"URL blocked or unreachable: {e}"})
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
