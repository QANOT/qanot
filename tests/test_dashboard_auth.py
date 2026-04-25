"""Tests for dashboard auth: token autogen, fail-fast on non-loopback, Origin check."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from qanot.dashboard import Dashboard, _is_loopback_bind


class _StubAgent:
    class _Ctx:
        def session_status(self):
            return {
                "context_percent": 0.0,
                "total_tokens": 0,
                "turn_count": 0,
                "api_calls": 0,
                "buffer_active": False,
            }

    class _ConvManager:
        def active_count(self):
            return 0

    def __init__(self):
        self.context = self._Ctx()
        self._conv_manager = self._ConvManager()
        self.cost_tracker = type("T", (), {"get_total_cost": lambda s: 0.0, "get_all_stats": lambda s: {}})()
        self.tools = type("T", (), {"get_definitions": lambda s: []})()
        self.provider = type("P", (), {})()


class _StubConfig:
    def __init__(self, **overrides):
        self.bot_name = "test"
        self.model = "test"
        self.provider = "test"
        self.response_mode = "blocked"
        self.voice_mode = "off"
        self.voice_provider = ""
        self.rag_enabled = False
        self.routing_enabled = False
        self.exec_security = "cautious"
        self.max_context_tokens = 100000
        self.heartbeat_enabled = False
        self.workspace_dir = "/tmp"
        self.dashboard_token = ""
        self.dashboard_host = "127.0.0.1"
        self.dashboard_allowed_origins: list[str] = []
        for k, v in overrides.items():
            setattr(self, k, v)


def test_is_loopback_bind_recognises_common_forms():
    assert _is_loopback_bind("127.0.0.1")
    assert _is_loopback_bind("127.0.0.5")  # any 127.x is loopback
    assert _is_loopback_bind("::1")
    assert _is_loopback_bind("localhost")
    assert not _is_loopback_bind("0.0.0.0")
    assert not _is_loopback_bind("192.168.1.1")
    assert not _is_loopback_bind("example.com")


@pytest.mark.asyncio
async def test_start_refuses_non_loopback_without_token():
    config = _StubConfig(dashboard_host="0.0.0.0", dashboard_token="")
    dash = Dashboard(config, _StubAgent())
    with pytest.raises(RuntimeError, match="non-loopback"):
        await dash.start(port=0, host="0.0.0.0")


@pytest.mark.asyncio
async def test_start_autogens_token_on_loopback():
    config = _StubConfig(dashboard_host="127.0.0.1", dashboard_token="")
    dash = Dashboard(config, _StubAgent())
    # Use port 0 so kernel assigns; host loopback so we don't refuse
    await dash.start(port=0, host="127.0.0.1")
    assert config.dashboard_token != ""
    assert len(config.dashboard_token) == 48  # 24 bytes hex = 48 chars


async def _make_client(config, agent) -> TestClient:
    dash = Dashboard(config, agent)
    server = TestServer(dash.app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_no_token_returns_401_when_middleware_active():
    """If dashboard_token is empty at request time, all dashboard routes 401."""
    config = _StubConfig(dashboard_token="")
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get("/api/status")
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_valid_token_passes():
    config = _StubConfig(dashboard_token="abc123")
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get("/api/status", headers={"Authorization": "Bearer abc123"})
        assert resp.status == 200
        resp2 = await client.get("/api/status?token=abc123")
        assert resp2.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_invalid_token_401():
    config = _StubConfig(dashboard_token="abc123")
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get("/api/status", headers={"Authorization": "Bearer wrong"})
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_origin_blocked_for_non_allowlisted_browser_request():
    config = _StubConfig(dashboard_token="abc123", dashboard_allowed_origins=[])
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get(
            "/api/status",
            headers={
                "Authorization": "Bearer abc123",
                "Origin": "https://evil.example.com",
                "Host": "qanot.example.com",
            },
        )
        assert resp.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_origin_allowed_when_in_allowlist():
    config = _StubConfig(
        dashboard_token="abc123",
        dashboard_allowed_origins=["https://my.qanot.local"],
    )
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get(
            "/api/status",
            headers={
                "Authorization": "Bearer abc123",
                "Origin": "https://my.qanot.local",
            },
        )
        assert resp.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_origin_loopback_always_passes():
    config = _StubConfig(dashboard_token="abc123")
    client = await _make_client(config, _StubAgent())
    try:
        for origin in ("http://127.0.0.1:8080", "http://localhost", "http://[::1]"):
            resp = await client.get(
                "/api/status",
                headers={"Authorization": "Bearer abc123", "Origin": origin},
            )
            assert resp.status == 200, f"loopback origin {origin!r} should pass"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_no_origin_header_passes_with_valid_token():
    """CLI / curl-style requests don't send Origin; should still work."""
    config = _StubConfig(dashboard_token="abc123")
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get("/api/status", headers={"Authorization": "Bearer abc123"})
        assert resp.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_webhook_path_bypasses_auth():
    """Webhook routes register on the same app and have their own auth."""
    config = _StubConfig(dashboard_token="abc123")
    dash = Dashboard(config, _StubAgent())

    async def fake_webhook(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    dash.app.router.add_post("/api/webhook", fake_webhook)

    server = TestServer(dash.app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.post("/api/webhook", json={})
        assert resp.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_origin_wildcard_allows_anything():
    config = _StubConfig(dashboard_token="abc123", dashboard_allowed_origins=["*"])
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get(
            "/api/status",
            headers={"Authorization": "Bearer abc123", "Origin": "https://anywhere.example"},
        )
        assert resp.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_same_host_origin_fallback():
    """Origin host == Host header → allowed (defense-in-depth, token still required)."""
    config = _StubConfig(dashboard_token="abc123")
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get(
            "/api/status",
            headers={
                "Authorization": "Bearer abc123",
                "Origin": "https://qanot.example.com:8765",
                "Host": "qanot.example.com",
            },
        )
        assert resp.status == 200
    finally:
        await client.close()
