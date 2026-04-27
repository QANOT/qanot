"""Tests for dashboard auth: token autogen, fail-fast on non-loopback, Origin check."""

from __future__ import annotations

from typing import Any

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
        self.video_render_url = ""
        self.video_service_secret = ""
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


@pytest.mark.asyncio
async def test_health_endpoint_is_public_no_token():
    """Docker HEALTHCHECK + k8s liveness probes must reach /api/health
    without knowing the auto-generated token. This was the regression that
    flipped containers to 'unhealthy' after the dashboard auth fix shipped."""
    config = _StubConfig(dashboard_token="some-token")
    client = await _make_client(config, _StubAgent())
    try:
        # No Authorization, no Origin — exactly what `curl -sf` from the
        # Dockerfile HEALTHCHECK sends.
        resp = await client.get("/api/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        assert "uptime_seconds" in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_health_endpoint_no_pii_leak():
    """Health payload must contain ONLY liveness fields — no config, no
    bot name, no token, no internal paths. Allowlist via key set."""
    config = _StubConfig(dashboard_token="some-token", bot_name="secret-bot")
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get("/api/health")
        body = await resp.json()
        assert set(body.keys()) <= {"ok", "uptime_seconds"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_health_endpoint_works_with_empty_token():
    """If somehow the token is cleared at runtime, /api/health still
    answers — it's the liveness probe that proves the process is alive."""
    config = _StubConfig(dashboard_token="")
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get("/api/health")
        assert resp.status == 200
    finally:
        await client.close()


# ── /api/video proxy ─────────────────────────────────────────────


async def _spawn_fake_render_service(
    payload: dict[str, Any] | None = None,
    status: int = 200,
    expect_bearer: str | None = None,
) -> tuple[TestServer, str]:
    """Boot a tiny aiohttp server that mimics qanot-video's GET /summary.

    Returns (server, base_url) so the test can wire it into config and
    tear it down on completion.
    """
    body = payload or {
        "queue_depth": 0,
        "worker_busy": False,
        "jobs_today": {"succeeded": 14, "failed": 1, "cancelled": 0},
        "recent_jobs": [
            {
                "job_id": "01HAAAAAAAAAAAAAAAAAAAAAAA",
                "bot_id": "topkeydevbot",
                "status": "succeeded",
                "duration_s": 38,
                "format": "9:16",
                "queued_at": "2026-04-26T08:00:00Z",
            }
        ],
        "disk_free_bytes": 8_500_000_000,
        "service_healthy": True,
    }

    async def handler(request: web.Request) -> web.Response:
        if expect_bearer is not None:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {expect_bearer}":
                return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(body, status=status)

    fake_app = web.Application()
    fake_app.router.add_get("/summary", handler)
    server = TestServer(fake_app)
    await server.start_server()
    base_url = f"http://127.0.0.1:{server.port}"
    return server, base_url


@pytest.mark.asyncio
async def test_api_video_proxies_summary_when_service_healthy():
    """Happy path: dashboard.config has render URL + secret; /api/video
    fetches /summary and returns the same JSON 1:1."""
    fake_server, base_url = await _spawn_fake_render_service(
        expect_bearer="render-secret-xyz",
    )
    try:
        config = _StubConfig(
            dashboard_token="dash-token",
            video_render_url=base_url,
            video_service_secret="render-secret-xyz",
        )
        client = await _make_client(config, _StubAgent())
        try:
            resp = await client.get(
                "/api/video",
                headers={"Authorization": "Bearer dash-token"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["service_healthy"] is True
            assert body["queue_depth"] == 0
            assert body["jobs_today"]["succeeded"] == 14
            assert body["disk_free_bytes"] == 8_500_000_000
            assert isinstance(body["recent_jobs"], list)
            assert body["recent_jobs"][0]["status"] == "succeeded"
        finally:
            await client.close()
    finally:
        await fake_server.close()


@pytest.mark.asyncio
async def test_api_video_returns_unhealthy_when_service_unreachable():
    """If the render service is down, dashboard returns 200 with
    service_healthy=false. The dashboard itself stays alive."""
    config = _StubConfig(
        dashboard_token="dash-token",
        # Bind to a port nothing is listening on; aiohttp ClientError fires.
        video_render_url="http://127.0.0.1:1",
        video_service_secret="render-secret-xyz",
    )
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get(
            "/api/video",
            headers={"Authorization": "Bearer dash-token"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["service_healthy"] is False
        assert "error" in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_video_unhealthy_when_unconfigured():
    """No render URL or secret in config → service_healthy=false. This
    is the default for bots that never enabled the video engine."""
    config = _StubConfig(
        dashboard_token="dash-token",
        video_render_url="",
        video_service_secret="",
    )
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get(
            "/api/video",
            headers={"Authorization": "Bearer dash-token"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["service_healthy"] is False
        assert "error" in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_video_unhealthy_when_service_returns_5xx():
    """Render service alive but reporting itself unhealthy (e.g. db
    corruption) → dashboard surfaces service_healthy=false."""
    fake_server, base_url = await _spawn_fake_render_service(
        payload={"error": "internal"},
        status=500,
    )
    try:
        config = _StubConfig(
            dashboard_token="dash-token",
            video_render_url=base_url,
            video_service_secret="anything",
        )
        client = await _make_client(config, _StubAgent())
        try:
            resp = await client.get(
                "/api/video",
                headers={"Authorization": "Bearer dash-token"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["service_healthy"] is False
            assert "500" in body["error"]
        finally:
            await client.close()
    finally:
        await fake_server.close()


@pytest.mark.asyncio
async def test_api_video_requires_dashboard_auth():
    """The /api/video endpoint is auth-gated -- no token, no proxy."""
    config = _StubConfig(
        dashboard_token="dash-token",
        video_render_url="http://127.0.0.1:1",
        video_service_secret="x",
    )
    client = await _make_client(config, _StubAgent())
    try:
        resp = await client.get("/api/video")
        assert resp.status == 401
    finally:
        await client.close()
