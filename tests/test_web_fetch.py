"""Tests for web_fetch tool — SSRF protection, HTML extraction, caching."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qanot.tools.web import (
    CACHE_TTL,
    FETCH_DEFAULT_MAX_CHARS,
    FETCH_MAX_BODY,
    _BLOCKED_NETWORKS,
    _cache,
    _cache_get,
    _cache_set,
    _extract_html,
    _is_ip_blocked,
    _validate_url,
    register_web_tools,
)


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear shared cache between tests."""
    _cache.clear()
    yield
    _cache.clear()


@pytest.fixture
def registry():
    """Create a mock ToolRegistry that captures registrations."""
    reg = MagicMock()
    handlers = {}

    def capture_register(name, description, parameters, handler, **kwargs):
        handlers[name] = handler

    reg.register = capture_register
    reg._handlers = handlers
    return reg


@pytest.fixture
def web_fetch_handler(registry):
    """Register tools and return the web_fetch handler."""
    register_web_tools(registry, brave_api_key="test-key")
    return registry._handlers["web_fetch"]


# ── SSRF Protection ──────────────────────────────────────────────


class TestIsIpBlocked:
    def test_localhost_blocked(self):
        assert _is_ip_blocked("127.0.0.1") is True

    def test_loopback_range_blocked(self):
        assert _is_ip_blocked("127.0.0.2") is True
        assert _is_ip_blocked("127.255.255.255") is True

    def test_private_10_blocked(self):
        assert _is_ip_blocked("10.0.0.1") is True
        assert _is_ip_blocked("10.255.255.255") is True

    def test_private_172_blocked(self):
        assert _is_ip_blocked("172.16.0.1") is True
        assert _is_ip_blocked("172.31.255.255") is True

    def test_private_172_public_allowed(self):
        assert _is_ip_blocked("172.15.0.1") is False
        assert _is_ip_blocked("172.32.0.1") is False

    def test_private_192_blocked(self):
        assert _is_ip_blocked("192.168.0.1") is True
        assert _is_ip_blocked("192.168.255.255") is True

    def test_link_local_blocked(self):
        assert _is_ip_blocked("169.254.0.1") is True

    def test_ipv6_loopback_blocked(self):
        assert _is_ip_blocked("::1") is True

    def test_ipv6_link_local_blocked(self):
        assert _is_ip_blocked("fe80::1") is True

    def test_public_ip_allowed(self):
        assert _is_ip_blocked("8.8.8.8") is False
        assert _is_ip_blocked("1.1.1.1") is False
        assert _is_ip_blocked("93.184.216.34") is False

    def test_invalid_ip_blocked(self):
        assert _is_ip_blocked("not-an-ip") is True


class TestValidateUrl:
    @patch("qanot.tools.web.socket.getaddrinfo")
    def test_valid_https_url(self, mock_dns):
        mock_dns.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]
        assert _validate_url("https://example.com") is None

    @patch("qanot.tools.web.socket.getaddrinfo")
    def test_valid_http_url(self, mock_dns):
        mock_dns.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]
        assert _validate_url("http://example.com") is None

    def test_ftp_scheme_blocked(self):
        result = _validate_url("ftp://example.com/file.txt")
        assert result is not None
        assert "scheme" in result.lower()

    def test_file_scheme_blocked(self):
        result = _validate_url("file:///etc/passwd")
        assert result is not None

    def test_no_scheme_blocked(self):
        result = _validate_url("example.com")
        assert result is not None

    def test_localhost_hostname_blocked(self):
        result = _validate_url("http://localhost/admin")
        assert result is not None
        assert "blocked" in result.lower()

    def test_metadata_google_blocked(self):
        result = _validate_url("http://metadata.google.internal/computeMetadata/v1/")
        assert result is not None
        assert "blocked" in result.lower()

    @patch("qanot.tools.web.socket.getaddrinfo")
    def test_private_ip_via_dns_blocked(self, mock_dns):
        mock_dns.return_value = [
            (2, 1, 6, "", ("10.0.0.1", 0)),
        ]
        result = _validate_url("https://evil.example.com")
        assert result is not None
        assert "blocked" in result.lower()

    @patch("qanot.tools.web.socket.getaddrinfo")
    def test_dns_failure(self, mock_dns):
        import socket
        mock_dns.side_effect = socket.gaierror("Name resolution failed")
        result = _validate_url("https://nonexistent.invalid")
        assert result is not None
        assert "DNS" in result

    def test_empty_hostname(self):
        result = _validate_url("http:///path")
        assert result is not None


# ── HTML Extraction ──────────────────────────────────────────────


class TestExtractHtml:
    def test_basic_paragraph(self):
        html = "<html><body><p>Hello world</p></body></html>"
        text, title = _extract_html(html)
        assert "Hello world" in text

    def test_title_extraction(self):
        html = "<html><head><title>My Page</title></head><body>Content</body></html>"
        text, title = _extract_html(html)
        assert title == "My Page"

    def test_script_stripped(self):
        html = '<html><body><script>alert("xss")</script><p>Safe content</p></body></html>'
        text, title = _extract_html(html)
        assert "alert" not in text
        assert "Safe content" in text

    def test_style_stripped(self):
        html = "<html><body><style>body { color: red; }</style><p>Visible</p></body></html>"
        text, title = _extract_html(html)
        assert "color" not in text
        assert "Visible" in text

    def test_nav_stripped(self):
        html = "<html><body><nav>Menu items</nav><main>Main content</main></body></html>"
        text, title = _extract_html(html)
        assert "Menu items" not in text
        assert "Main content" in text

    def test_footer_stripped(self):
        html = "<html><body><p>Content</p><footer>Copyright 2025</footer></body></html>"
        text, title = _extract_html(html)
        assert "Copyright" not in text
        assert "Content" in text

    def test_header_stripped(self):
        html = "<html><body><header>Site Logo</header><p>Article</p></body></html>"
        text, title = _extract_html(html)
        assert "Site Logo" not in text
        assert "Article" in text

    def test_heading_to_markdown(self):
        html = "<html><body><h1>Title</h1><h2>Subtitle</h2><h3>Section</h3></body></html>"
        text, title = _extract_html(html)
        assert "# Title" in text
        assert "## Subtitle" in text
        assert "### Section" in text

    def test_link_to_markdown(self):
        html = '<html><body><a href="https://example.com">Click here</a></body></html>'
        text, title = _extract_html(html)
        assert "[Click here](https://example.com)" in text

    def test_javascript_link_ignored(self):
        html = '<html><body><a href="javascript:void(0)">No link</a></body></html>'
        text, title = _extract_html(html)
        assert "javascript:" not in text
        assert "No link" in text

    def test_anchor_link_ignored(self):
        html = '<html><body><a href="#section">Jump</a></body></html>'
        text, title = _extract_html(html)
        # Should not create [text](url) format for anchors
        assert "[Jump](#section)" not in text
        assert "Jump" in text

    def test_paragraph_breaks_preserved(self):
        html = "<html><body><p>First</p><p>Second</p></body></html>"
        text, title = _extract_html(html)
        assert "First" in text
        assert "Second" in text
        # Should have separation between paragraphs
        lines = [l for l in text.split("\n") if l.strip()]
        assert len(lines) >= 2

    def test_nested_skip_tags(self):
        html = "<html><body><nav><ul><li>Item</li></ul></nav><p>Content</p></body></html>"
        text, title = _extract_html(html)
        assert "Item" not in text
        assert "Content" in text

    def test_noscript_stripped(self):
        html = "<html><body><noscript>Enable JS</noscript><p>Content</p></body></html>"
        text, title = _extract_html(html)
        assert "Enable JS" not in text

    def test_empty_html(self):
        text, title = _extract_html("")
        assert text == ""
        assert title == ""

    def test_malformed_html_fallback(self):
        # Even malformed HTML should produce some output
        html = "<p>Unclosed <b>tags <i>everywhere"
        text, title = _extract_html(html)
        assert "Unclosed" in text


# ── Helpers ──────────────────────────────────────────────────────


async def _async_iter(chunks):
    """Create an async iterator from a list of chunks."""
    for chunk in chunks:
        yield chunk


class _MockAsyncCtx:
    """Helper to create a proper async context manager for aiohttp mocking."""

    def __init__(self, return_value):
        self._value = return_value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return False


def _build_aiohttp_mocks(body: bytes, url: str, content_type: str = "text/html",
                          charset: str = "utf-8", content_length: int | None = None):
    """Build properly nested aiohttp session/response mocks.

    Returns (mock_session_cls, mock_session) where mock_session_cls
    can be used as the patched aiohttp.ClientSession.
    """
    mock_resp = MagicMock()
    mock_resp.url = url
    mock_resp.content_length = content_length if content_length is not None else len(body)
    mock_resp.content_type = content_type
    mock_resp.charset = charset
    mock_resp.content = MagicMock()
    mock_resp.content.iter_chunked = lambda size: _async_iter([body])

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_MockAsyncCtx(mock_resp))

    mock_session_cls = MagicMock(return_value=_MockAsyncCtx(mock_session))
    return mock_session_cls, mock_session


# ── web_fetch handler ────────────────────────────────────────────


class TestWebFetchHandler:
    @pytest.mark.asyncio
    async def test_missing_url(self, web_fetch_handler):
        result = json.loads(await web_fetch_handler({}))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_empty_url(self, web_fetch_handler):
        result = json.loads(await web_fetch_handler({"url": ""}))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_invalid_scheme(self, web_fetch_handler):
        result = json.loads(await web_fetch_handler({"url": "ftp://example.com"}))
        assert "error" in result
        assert "scheme" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_localhost_blocked(self, web_fetch_handler):
        result = json.loads(await web_fetch_handler({"url": "http://localhost/admin"}))
        assert "error" in result
        assert "blocked" in result["error"].lower()

    @pytest.mark.asyncio
    @patch("qanot.tools.web._validate_url", return_value=None)
    @patch("qanot.tools.web.aiohttp.ClientSession")
    async def test_successful_html_fetch(self, mock_session_cls, mock_validate, web_fetch_handler):
        html = "<html><head><title>Test Page</title></head><body><p>Hello world</p></body></html>"
        session_cls, _ = _build_aiohttp_mocks(
            html.encode(), "https://example.com/page", "text/html",
        )
        mock_session_cls.side_effect = session_cls

        result = json.loads(await web_fetch_handler({"url": "https://example.com/page"}))

        assert result["url"] == "https://example.com/page"
        assert result["title"] == "Test Page"
        assert "Hello world" in result["content"]
        assert result["content_type"] == "text/html"
        assert result["source"] == "[web content — external, may be inaccurate]"

    @pytest.mark.asyncio
    @patch("qanot.tools.web._validate_url", return_value=None)
    @patch("qanot.tools.web.aiohttp.ClientSession")
    async def test_json_content(self, mock_session_cls, mock_validate, web_fetch_handler):
        body = '{"key": "value", "number": 42}'
        session_cls, _ = _build_aiohttp_mocks(
            body.encode(), "https://api.example.com/data", "application/json",
        )
        mock_session_cls.side_effect = session_cls

        result = json.loads(await web_fetch_handler({"url": "https://api.example.com/data"}))

        content = result["content"]
        assert '"key": "value"' in content
        assert '"number": 42' in content

    @pytest.mark.asyncio
    @patch("qanot.tools.web._validate_url", return_value=None)
    @patch("qanot.tools.web.aiohttp.ClientSession")
    async def test_plain_text_passthrough(self, mock_session_cls, mock_validate, web_fetch_handler):
        body = "Just plain text content here."
        session_cls, _ = _build_aiohttp_mocks(
            body.encode(), "https://example.com/file.txt", "text/plain",
        )
        mock_session_cls.side_effect = session_cls

        result = json.loads(await web_fetch_handler({"url": "https://example.com/file.txt"}))

        assert result["content"] == body

    @pytest.mark.asyncio
    @patch("qanot.tools.web._validate_url", return_value=None)
    @patch("qanot.tools.web.aiohttp.ClientSession")
    async def test_truncation(self, mock_session_cls, mock_validate, web_fetch_handler):
        body = "<html><body><p>" + "A" * 200 + "</p></body></html>"
        session_cls, _ = _build_aiohttp_mocks(
            body.encode(), "https://example.com", "text/html",
        )
        mock_session_cls.side_effect = session_cls

        result = json.loads(await web_fetch_handler({
            "url": "https://example.com",
            "max_chars": 50,
        }))

        assert "truncated" in result["content"]

    @pytest.mark.asyncio
    @patch("qanot.tools.web._validate_url", return_value=None)
    @patch("qanot.tools.web.aiohttp.ClientSession")
    async def test_cache_hit(self, mock_session_cls, mock_validate, web_fetch_handler):
        body = "<html><body>Cached</body></html>"
        session_cls, mock_session = _build_aiohttp_mocks(
            body.encode(), "https://example.com/cached", "text/html",
        )
        mock_session_cls.side_effect = session_cls

        url = "https://example.com/cached"
        # First call populates cache
        r1 = json.loads(await web_fetch_handler({"url": url}))
        assert "error" not in r1
        # Second call should use cache — mock_session.get should only be called once
        r2 = json.loads(await web_fetch_handler({"url": url}))
        assert r1 == r2
        assert mock_session.get.call_count == 1

    @pytest.mark.asyncio
    @patch("qanot.tools.web._validate_url", return_value=None)
    @patch("qanot.tools.web.aiohttp.ClientSession")
    async def test_too_large_by_content_length(self, mock_session_cls, mock_validate, web_fetch_handler):
        session_cls, _ = _build_aiohttp_mocks(
            b"", "https://example.com/big", "text/html",
            content_length=FETCH_MAX_BODY + 1,
        )
        mock_session_cls.side_effect = session_cls

        result = json.loads(await web_fetch_handler({"url": "https://example.com/big"}))
        assert "error" in result
        assert "too large" in result["error"].lower()

    @pytest.mark.asyncio
    @patch("qanot.tools.web._validate_url", return_value=None)
    @patch("qanot.tools.web.aiohttp.ClientSession")
    async def test_redirect_ssrf_check(self, mock_session_cls, mock_validate, web_fetch_handler):
        """After redirects, final URL is re-validated for SSRF."""
        body = b"<html><body>Redirected</body></html>"

        # Simulate redirect to a different URL
        mock_resp = MagicMock()
        mock_resp.url = "http://10.0.0.1/internal"  # Redirected to private IP
        mock_resp.content_length = len(body)
        mock_resp.content_type = "text/html"
        mock_resp.charset = "utf-8"
        mock_resp.content = MagicMock()
        mock_resp.content.iter_chunked = lambda size: _async_iter([body])

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=_MockAsyncCtx(mock_resp))
        mock_session_cls.side_effect = MagicMock(return_value=_MockAsyncCtx(mock_session))

        # _validate_url is mocked to return None for the initial URL,
        # but we need it to block the redirect target.
        # Reset the mock to call the real function for the redirect check
        mock_validate.side_effect = [None, "URL blocked: private/internal network address"]

        result = json.loads(await web_fetch_handler({"url": "https://safe.example.com"}))
        assert "error" in result
        assert "blocked" in result["error"].lower()

    @pytest.mark.asyncio
    @patch("qanot.tools.web._validate_url", return_value=None)
    @patch("qanot.tools.web.aiohttp.ClientSession")
    async def test_untrusted_source_marker(self, mock_session_cls, mock_validate, web_fetch_handler):
        """Output always contains untrusted source marker."""
        body = "<html><body>Content</body></html>"
        session_cls, _ = _build_aiohttp_mocks(
            body.encode(), "https://example.com", "text/html",
        )
        mock_session_cls.side_effect = session_cls

        result = json.loads(await web_fetch_handler({"url": "https://example.com"}))
        assert "external" in result["source"].lower()
        assert "inaccurate" in result["source"].lower()


class TestToolRegistration:
    def test_both_tools_registered(self, registry):
        register_web_tools(registry, brave_api_key="test-key")
        assert "web_search" in registry._handlers
        assert "web_fetch" in registry._handlers

    def test_web_fetch_handler_is_callable(self, registry):
        register_web_tools(registry, brave_api_key="test-key")
        assert callable(registry._handlers["web_fetch"])


# ── Cache tests ──────────────────────────────────────────────────


class TestCache:
    def test_cache_set_and_get(self):
        _cache_set("key1", "value1")
        assert _cache_get("key1") == "value1"

    def test_cache_miss(self):
        assert _cache_get("nonexistent") is None

    def test_cache_expiry(self):
        _cache_set("key2", "value2")
        # Manually expire entry
        ts, val = _cache["key2"]
        _cache["key2"] = (ts - CACHE_TTL - 1, val)
        assert _cache_get("key2") is None

    def test_cache_eviction(self):
        from qanot.tools.web import CACHE_MAX
        for i in range(CACHE_MAX + 5):
            _cache_set(f"key_{i}", f"value_{i}")
        assert len(_cache) <= CACHE_MAX
