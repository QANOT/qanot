"""Tests for auto link understanding."""

from __future__ import annotations

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from qanot.links import extract_urls, _should_skip_url, fetch_link_previews


class TestExtractUrls:

    def test_extracts_http_urls(self):
        text = "Check out http://example.com for more info"
        assert extract_urls(text) == ["http://example.com"]

    def test_extracts_https_urls(self):
        text = "Visit https://docs.python.org/3/library.html"
        assert extract_urls(text) == ["https://docs.python.org/3/library.html"]

    def test_extracts_multiple_urls(self):
        text = "See https://a.com and https://b.com"
        urls = extract_urls(text)
        assert len(urls) == 2
        assert "https://a.com" in urls
        assert "https://b.com" in urls

    def test_deduplicates(self):
        text = "https://a.com and again https://a.com"
        assert extract_urls(text) == ["https://a.com"]

    def test_strips_trailing_punctuation(self):
        text = "Go to https://example.com."
        assert extract_urls(text) == ["https://example.com"]

    def test_strips_trailing_comma(self):
        text = "See https://a.com, https://b.com, and more"
        urls = extract_urls(text)
        assert "https://a.com" in urls
        assert "https://b.com" in urls

    def test_no_urls(self):
        assert extract_urls("just plain text") == []
        assert extract_urls("") == []

    def test_preserves_query_params(self):
        text = "https://search.com/q?term=hello&page=1"
        urls = extract_urls(text)
        assert urls == ["https://search.com/q?term=hello&page=1"]


class TestShouldSkipUrl:

    def test_skips_images(self):
        assert _should_skip_url("https://example.com/photo.png")
        assert _should_skip_url("https://example.com/photo.jpg")
        assert _should_skip_url("https://example.com/photo.gif")

    def test_skips_videos(self):
        assert _should_skip_url("https://example.com/video.mp4")
        assert _should_skip_url("https://example.com/video.webm")

    def test_skips_archives(self):
        assert _should_skip_url("https://example.com/file.zip")
        assert _should_skip_url("https://example.com/file.tar.gz")

    def test_skips_telegram(self):
        assert _should_skip_url("https://t.me/channel")

    def test_skips_api_endpoints(self):
        assert _should_skip_url("https://api.example.com/data")
        assert _should_skip_url("https://example.com/api/v2/users")

    def test_skips_localhost(self):
        assert _should_skip_url("http://localhost:3000")
        assert _should_skip_url("http://127.0.0.1:8080")

    def test_allows_normal_urls(self):
        assert not _should_skip_url("https://example.com")
        assert not _should_skip_url("https://docs.python.org/3/library.html")
        assert not _should_skip_url("https://news.ycombinator.com")

    def test_handles_query_params_after_extension(self):
        assert _should_skip_url("https://example.com/file.pdf?token=abc")


class TestFetchLinkPreviews:

    @pytest.mark.asyncio
    async def test_empty_text_returns_empty(self):
        result = await fetch_link_previews("")
        assert result == ""

    @pytest.mark.asyncio
    async def test_no_urls_returns_empty(self):
        result = await fetch_link_previews("just plain text with no links")
        assert result == ""

    @pytest.mark.asyncio
    async def test_skipped_urls_returns_empty(self):
        result = await fetch_link_previews("Check https://t.me/channel")
        assert result == ""

    @pytest.mark.asyncio
    async def test_max_urls_limit(self):
        text = " ".join(f"https://example{i}.com" for i in range(10))
        with patch("qanot.links.fetch_url_preview", new_callable=AsyncMock) as mock:
            mock.return_value = None
            await fetch_link_previews(text, max_urls=3)
            assert mock.call_count == 3

    @pytest.mark.asyncio
    async def test_formats_preview_output(self):
        text = "Check https://example.com"
        with patch("qanot.links.fetch_url_preview", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "url": "https://example.com",
                "title": "Example",
                "preview": "This is the example page content.",
            }
            result = await fetch_link_previews(text)
            assert "LINK CONTEXT" in result
            assert "https://example.com" in result
            assert "Example" in result
            assert "This is the example page content." in result

    @pytest.mark.asyncio
    async def test_handles_fetch_failure_gracefully(self):
        text = "Check https://example.com"
        with patch("qanot.links.fetch_url_preview", new_callable=AsyncMock) as mock:
            mock.side_effect = Exception("network error")
            result = await fetch_link_previews(text)
            assert result == ""
