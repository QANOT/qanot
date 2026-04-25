"""Per-user hourly rate limits on expensive tools.

Bill-leak protection: web_search hits Brave (~$0.01/query) and
generate_image hits Gemini Nano Banana Pro (~$0.04/image). Without
per-user caps, prompt injection or a malicious user can drive a $100+
bill in minutes through the agent loop's 25 iterations per turn.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qanot.registry import ToolRegistry
from qanot.tools.image import register_image_tools
from qanot.tools.web import _cache, register_web_tools


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_web_cache():
    """Clear shared web search cache between tests."""
    _cache.clear()
    yield
    _cache.clear()


def _stub_brave_session(payload: dict | None = None):
    """Build a MagicMock that drops in for aiohttp.ClientSession.

    Returns a 200 response with the given payload (default = single dummy
    result) so the handler completes the success path without touching
    the network.
    """
    payload = payload or {
        "web": {
            "results": [
                {
                    "title": "stub",
                    "url": "https://example.com",
                    "description": "stub",
                }
            ]
        }
    }

    response = MagicMock()
    response.status = 200
    response.json = AsyncMock(return_value=payload)

    response_ctx = MagicMock()
    response_ctx.__aenter__ = AsyncMock(return_value=response)
    response_ctx.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.get = MagicMock(return_value=response_ctx)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=None)

    return session_ctx


# ── web_search ────────────────────────────────────────────────────


class TestWebSearchRateLimit:
    """Per-user hourly cap on web_search."""

    @pytest.mark.asyncio
    async def test_blocks_after_limit(self):
        """20 calls succeed, the 21st through 25th return rate_limited error."""
        registry = ToolRegistry()
        current_user = {"id": "u1"}
        register_web_tools(
            registry,
            brave_api_key="test-key",
            get_user_id=lambda: current_user["id"],
            per_user_hourly=20,
        )

        with patch(
            "qanot.tools.web.aiohttp.ClientSession",
            return_value=_stub_brave_session(),
        ):
            successes = 0
            rate_limited = 0
            for i in range(25):
                # Vary the query so cache doesn't short-circuit and
                # bypass the rate limit.
                result = await registry.execute(
                    "web_search", {"query": f"test query {i}", "count": 1}
                )
                data = json.loads(result)
                if data.get("error") == "rate_limited":
                    rate_limited += 1
                    assert "20" in data["reason"]
                    assert isinstance(data["retry_after_seconds"], int)
                    assert data["retry_after_seconds"] > 0
                else:
                    successes += 1

        assert successes == 20
        assert rate_limited == 5

    @pytest.mark.asyncio
    async def test_per_user_isolation(self):
        """One user hitting the cap doesn't affect a different user."""
        registry = ToolRegistry()
        current_user = {"id": "u1"}
        register_web_tools(
            registry,
            brave_api_key="test-key",
            get_user_id=lambda: current_user["id"],
            per_user_hourly=20,
        )

        with patch(
            "qanot.tools.web.aiohttp.ClientSession",
            return_value=_stub_brave_session(),
        ):
            # Burn u1's full hourly budget
            for i in range(25):
                await registry.execute(
                    "web_search", {"query": f"u1 query {i}", "count": 1}
                )

            # Switch user and confirm a fresh request succeeds
            current_user["id"] = "u2"
            result = await registry.execute(
                "web_search", {"query": "u2 fresh query", "count": 1}
            )
            data = json.loads(result)
            assert data.get("error") != "rate_limited"
            assert "results" in data

    @pytest.mark.asyncio
    async def test_zero_limit_disables(self):
        """per_user_hourly=0 must skip rate limiting entirely (opt-out)."""
        registry = ToolRegistry()
        register_web_tools(
            registry,
            brave_api_key="test-key",
            get_user_id=lambda: "u1",
            per_user_hourly=0,
        )

        with patch(
            "qanot.tools.web.aiohttp.ClientSession",
            return_value=_stub_brave_session(),
        ):
            for i in range(50):
                result = await registry.execute(
                    "web_search", {"query": f"unlimited {i}", "count": 1}
                )
                data = json.loads(result)
                assert data.get("error") != "rate_limited"

    @pytest.mark.asyncio
    async def test_no_user_id_fails_open(self):
        """When user_id is None (system caller), rate limit is skipped."""
        registry = ToolRegistry()
        register_web_tools(
            registry,
            brave_api_key="test-key",
            get_user_id=lambda: None,
            per_user_hourly=2,
        )

        with patch(
            "qanot.tools.web.aiohttp.ClientSession",
            return_value=_stub_brave_session(),
        ):
            for i in range(5):
                result = await registry.execute(
                    "web_search", {"query": f"sys query {i}", "count": 1}
                )
                data = json.loads(result)
                assert data.get("error") != "rate_limited"

    @pytest.mark.asyncio
    async def test_no_get_user_id_callable_disables(self):
        """Without a get_user_id callable at all, rate limit is skipped."""
        registry = ToolRegistry()
        register_web_tools(
            registry,
            brave_api_key="test-key",
            per_user_hourly=2,
        )

        with patch(
            "qanot.tools.web.aiohttp.ClientSession",
            return_value=_stub_brave_session(),
        ):
            for i in range(5):
                result = await registry.execute(
                    "web_search", {"query": f"no-uid {i}", "count": 1}
                )
                data = json.loads(result)
                assert data.get("error") != "rate_limited"

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_count(self):
        """Cached results don't hit Brave so they shouldn't burn quota."""
        registry = ToolRegistry()
        register_web_tools(
            registry,
            brave_api_key="test-key",
            get_user_id=lambda: "u1",
            per_user_hourly=3,
        )

        with patch(
            "qanot.tools.web.aiohttp.ClientSession",
            return_value=_stub_brave_session(),
        ):
            # First call: real, counts (1/3)
            await registry.execute(
                "web_search", {"query": "cached query", "count": 1}
            )
            # 50 cache hits — must not consume the remaining 2 slots
            for _ in range(50):
                result = await registry.execute(
                    "web_search", {"query": "cached query", "count": 1}
                )
                data = json.loads(result)
                assert data.get("error") != "rate_limited"

            # Now two distinct queries should still succeed (slots 2 & 3)
            for i in range(2):
                result = await registry.execute(
                    "web_search", {"query": f"distinct {i}", "count": 1}
                )
                data = json.loads(result)
                assert data.get("error") != "rate_limited"

            # The 4th distinct query trips the cap
            result = await registry.execute(
                "web_search", {"query": "tripping query", "count": 1}
            )
            data = json.loads(result)
            assert data.get("error") == "rate_limited"


# ── generate_image ────────────────────────────────────────────────


class _StubInlineData:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _StubPart:
    def __init__(self, data: bytes | None = None, text: str = "") -> None:
        self.inline_data = _StubInlineData(data) if data else None
        self.text = text


class _StubContent:
    def __init__(self, parts: list[_StubPart]) -> None:
        self.parts = parts


class _StubCandidate:
    def __init__(self, parts: list[_StubPart]) -> None:
        self.content = _StubContent(parts)


class _StubResponse:
    def __init__(self, parts: list[_StubPart]) -> None:
        self.candidates = [_StubCandidate(parts)]


def _stub_genai_client():
    """Build a fake genai.Client that returns a 1-byte image."""
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(
        return_value=_StubResponse(
            [_StubPart(data=b"\x89PNG\r\n\x1a\n", text="ok")]
        )
    )
    return client


def _patch_genai_modules():
    """Build a sys.modules patch dict for the lazy google.genai imports.

    The image handler does ``from google import genai`` and
    ``from google.genai import types`` inside its body; both need to
    resolve to mocks so the success path runs without the real SDK.
    """
    fake_genai = MagicMock()
    fake_genai.Client = MagicMock(return_value=_stub_genai_client())
    fake_types = MagicMock()
    fake_types.GenerateContentConfig = MagicMock()

    # `from google import genai` reads google.genai off the package
    fake_google = MagicMock()
    fake_google.genai = fake_genai

    # `from google.genai import types` imports google.genai.types
    fake_genai.types = fake_types

    return {
        "google": fake_google,
        "google.genai": fake_genai,
        "google.genai.types": fake_types,
    }


class TestGenerateImageRateLimit:
    """Per-user hourly cap on generate_image."""

    @pytest.mark.asyncio
    async def test_blocks_after_limit(self, tmp_path):
        """10 calls succeed, the 11th through 15th return rate_limited."""
        registry = ToolRegistry()
        current_user = {"id": "u1"}
        register_image_tools(
            registry,
            api_key="fake-key",
            workspace_dir=str(tmp_path),
            get_user_id=lambda: current_user["id"],
            per_user_hourly=10,
        )

        with patch.dict("sys.modules", _patch_genai_modules()):
            successes = 0
            rate_limited = 0
            for i in range(15):
                result = await registry.execute(
                    "generate_image", {"prompt": f"test image {i}"}
                )
                data = json.loads(result)
                if data.get("error") == "rate_limited":
                    rate_limited += 1
                    assert "10" in data["reason"]
                    assert isinstance(data["retry_after_seconds"], int)
                    assert data["retry_after_seconds"] > 0
                else:
                    # Must have succeeded — anything else is a real bug
                    assert data.get("status") == "ok", data
                    successes += 1

        assert successes == 10
        assert rate_limited == 5

    @pytest.mark.asyncio
    async def test_per_user_isolation(self, tmp_path):
        """u1 hitting the cap doesn't block u2."""
        registry = ToolRegistry()
        current_user = {"id": "u1"}
        register_image_tools(
            registry,
            api_key="fake-key",
            workspace_dir=str(tmp_path),
            get_user_id=lambda: current_user["id"],
            per_user_hourly=10,
        )

        with patch.dict("sys.modules", _patch_genai_modules()):
            # Burn u1's quota
            for i in range(15):
                await registry.execute(
                    "generate_image", {"prompt": f"u1 image {i}"}
                )

            # Different user — should succeed
            current_user["id"] = "u2"
            result = await registry.execute(
                "generate_image", {"prompt": "u2 fresh image"}
            )
            data = json.loads(result)
            assert data.get("error") != "rate_limited"
            assert data.get("status") == "ok"

    @pytest.mark.asyncio
    async def test_zero_limit_disables(self, tmp_path):
        """per_user_hourly=0 = unlimited (opt-out)."""
        registry = ToolRegistry()
        register_image_tools(
            registry,
            api_key="fake-key",
            workspace_dir=str(tmp_path),
            get_user_id=lambda: "u1",
            per_user_hourly=0,
        )

        with patch.dict("sys.modules", _patch_genai_modules()):
            for i in range(20):
                result = await registry.execute(
                    "generate_image", {"prompt": f"unlimited {i}"}
                )
                data = json.loads(result)
                assert data.get("error") != "rate_limited"

    @pytest.mark.asyncio
    async def test_no_user_id_fails_open(self, tmp_path):
        """user_id None (system caller) skips rate limit."""
        registry = ToolRegistry()
        register_image_tools(
            registry,
            api_key="fake-key",
            workspace_dir=str(tmp_path),
            get_user_id=lambda: None,
            per_user_hourly=2,
        )

        with patch.dict("sys.modules", _patch_genai_modules()):
            for i in range(5):
                result = await registry.execute(
                    "generate_image", {"prompt": f"sys image {i}"}
                )
                data = json.loads(result)
                assert data.get("error") != "rate_limited"
