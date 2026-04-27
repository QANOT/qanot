"""Tests for qanot.tools.video — render_video tool layer.

Covers per-architecture-spec §4 (Python Bridge) requirements:

- composition prompt building (skill + DESIGN.md + format/duration)
- composition validation (must start with <!doctype)
- per-user / per-bot daily rate limit
- per-user / per-bot daily cost cap
- service submit retry policy on network failure
- service rejection on 4xx
- successful render flow end-to-end
- lint_failed retry-with-feedback (exhausted)
- error code mapping
- registration toggle (off / legacy_reels / hyperframes)
- Agent.pop_pending_videos plumbing

The render service is mocked at the httpx layer; we never spin up an actual
service in unit tests.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from qanot.tools.video import (
    _COMPOSITION_RETRY_BUDGET,
    _CostLedger,
    _DailyCounter,
    _ServiceUnavailable,
    _author_composition,
    _build_composition_system_prompt,
    _estimate_composition_cost_micros,
    _format_to_dimensions,
    _poll_job,
    _submit_render,
    _validate_composition_html,
    register_video_tools,
)


# ── Helpers ─────────────────────────────────────────────────────────────


@dataclass
class _StubConfig:
    bot_name: str = "test-bot"
    workspace_dir: str = ""  # set by tests via tmp_path
    video_engine: str = "hyperframes"
    video_render_url: str = "http://test.local:8770"
    video_service_secret: str = "test-secret"
    video_per_user_daily_limit: int = 5
    video_per_bot_daily_limit: int = 50
    video_per_user_daily_cost_usd: float = 0.50
    video_per_bot_daily_cost_usd: float = 5.00
    video_composition_model: str = "claude-sonnet-4-6"
    video_default_duration_seconds: int = 30
    video_max_duration_seconds: int = 60


class _FakeResponse:
    def __init__(self, content: str = "<!doctype html><html></html>"):
        self.content = content


class _FakeProvider:
    """Returns a canned composition. Tracks call_count for assertions."""

    def __init__(self, response_text: str = "<!doctype html><html><body>x</body></html>"):
        self.response_text = response_text
        self.call_count = 0
        self.last_messages: list[dict[str, Any]] = []
        self.last_system: str = ""

    async def chat(self, *, messages, system, tools, model, max_tokens):
        self.call_count += 1
        self.last_messages = messages
        self.last_system = system
        return SimpleNamespace(content=self.response_text)


@pytest.fixture(autouse=True)
def _clear_skill_cache():
    """Skill cache is module-level; reset between tests so DESIGN/SKILL
    overrides are honored."""
    from qanot.tools import video as vmod
    vmod._skill_text_cache = None
    yield
    vmod._skill_text_cache = None


@pytest.fixture
def _agent_singleton():
    """Provide a stub Agent._instance with a pop_pending_videos / provider."""
    from qanot.agent import Agent

    @dataclass
    class _Agent:
        provider: Any
        current_user_id: str = "u1"
        current_chat_id: int = 1
        _pending_videos: dict = field(default_factory=dict)

        @classmethod
        def _push_pending_video(cls, user_id: str, path: str) -> None:
            inst = Agent._instance
            if inst is not None:
                inst._pending_videos.setdefault(user_id, []).append(path)

        def pop_pending_videos(self, user_id: str) -> list[str]:
            return self._pending_videos.pop(user_id, [])

    provider = _FakeProvider()
    a = _Agent(provider=provider)
    saved_instance = Agent._instance
    Agent._instance = a  # type: ignore[assignment]
    yield a
    Agent._instance = saved_instance


# ── Composition prompt + validation ─────────────────────────────────────


class TestCompositionPrompt:
    def test_format_to_dimensions(self):
        assert _format_to_dimensions("9:16") == (1080, 1920)
        assert _format_to_dimensions("16:9") == (1920, 1080)
        assert _format_to_dimensions("1:1") == (1080, 1080)

    def test_format_unsupported_raises(self):
        with pytest.raises(ValueError):
            _format_to_dimensions("4:3")

    def test_prompt_includes_skill_and_design_when_present(self):
        prompt = _build_composition_system_prompt(
            skill="SKILL CONTENT HERE", design="DESIGN CONTENT HERE",
            duration_seconds=15, aspect_format="9:16",
        )
        assert "SKILL CONTENT HERE" in prompt
        assert "DESIGN CONTENT HERE" in prompt
        assert "Brand for this bot" in prompt
        assert "exactly 15 seconds" in prompt
        assert 'data-width="1080"' in prompt and 'data-height="1920"' in prompt

    def test_prompt_omits_design_when_absent(self):
        prompt = _build_composition_system_prompt(
            skill="SKILL", design="", duration_seconds=10, aspect_format="9:16",
        )
        assert "Brand for this bot" not in prompt

    def test_prompt_omits_skill_section_when_skill_empty(self):
        prompt = _build_composition_system_prompt(
            skill="", design="", duration_seconds=10, aspect_format="9:16",
        )
        assert "HyperFrames composition guide" not in prompt

    def test_prompt_demands_doctype(self):
        prompt = _build_composition_system_prompt(
            skill="", design="", duration_seconds=10, aspect_format="9:16",
        )
        assert "<!doctype html>" in prompt


class TestValidateComposition:
    def test_accepts_doctype_lowercase(self):
        ok, _ = _validate_composition_html("<!doctype html><html></html>")
        assert ok

    def test_accepts_doctype_uppercase(self):
        ok, _ = _validate_composition_html("<!DOCTYPE HTML><html></html>")
        assert ok

    def test_accepts_with_leading_whitespace(self):
        ok, _ = _validate_composition_html("\n  <!doctype html>\n<html></html>")
        assert ok

    def test_rejects_empty(self):
        ok, reason = _validate_composition_html("")
        assert not ok and "empty" in reason

    def test_rejects_html_without_doctype(self):
        ok, reason = _validate_composition_html("<html><body></body></html>")
        assert not ok and "doctype" in reason

    def test_rejects_markdown_wrap(self):
        ok, reason = _validate_composition_html("```html\n<html></html>\n```")
        assert not ok


# ── Author composition (provider call) ─────────────────────────────────


class TestAuthorComposition:
    @pytest.mark.asyncio
    async def test_returns_html_on_success(self):
        prov = _FakeProvider("<!doctype html><html></html>")
        html = await _author_composition(
            provider=prov,
            model="sonnet",
            system_prompt="sys",
            brief="make a video",
        )
        assert html.startswith("<!doctype")
        assert prov.call_count == 1

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self):
        prov = _FakeProvider("```html\n<!doctype html><html></html>\n```")
        html = await _author_composition(
            provider=prov,
            model="sonnet",
            system_prompt="sys",
            brief="brief",
        )
        assert html.startswith("<!doctype")
        assert "```" not in html

    @pytest.mark.asyncio
    async def test_raises_on_invalid_output(self):
        prov = _FakeProvider("This is not HTML")
        with pytest.raises(ValueError, match="composition invalid"):
            await _author_composition(
                provider=prov,
                model="sonnet",
                system_prompt="sys",
                brief="brief",
            )

    @pytest.mark.asyncio
    async def test_appends_feedback_on_retry(self):
        prov = _FakeProvider("<!doctype html><html></html>")
        await _author_composition(
            provider=prov,
            model="sonnet",
            system_prompt="sys",
            brief="brief",
            feedback="missing data-duration",
        )
        # Feedback shows up in the user message for the retry call.
        assert "Previous attempt failed lint" in prov.last_messages[0]["content"]
        assert "missing data-duration" in prov.last_messages[0]["content"]


# ── Daily counters + cost ledger ────────────────────────────────────────


class TestDailyCounter:
    def test_zero_limit_disables(self):
        c = _DailyCounter(limit=0)
        assert c.check("u1") is True
        for _ in range(1000):
            c.record("u1")
        assert c.check("u1") is True

    def test_blocks_after_limit(self):
        c = _DailyCounter(limit=2)
        assert c.check("u1") is True
        c.record("u1")
        assert c.check("u1") is True
        c.record("u1")
        assert c.check("u1") is False
        # different key unaffected
        assert c.check("u2") is True

    def test_independent_keys(self):
        c = _DailyCounter(limit=1)
        c.record("u1")
        assert c.check("u1") is False
        assert c.check("u2") is True


class TestCostLedger:
    def test_zero_cap_disables(self):
        l = _CostLedger(cap_usd=0.0)
        assert l.remaining_micros("u1") > 0
        l.add("u1", 10**9)  # spending way over should still leave budget
        assert l.remaining_micros("u1") > 0

    def test_blocks_when_budget_exhausted(self):
        l = _CostLedger(cap_usd=1.0)  # 1 USD = 1e6 micros
        assert l.remaining_micros("u1") == 1_000_000
        l.add("u1", 600_000)
        assert l.remaining_micros("u1") == 400_000
        l.add("u1", 500_000)
        assert l.remaining_micros("u1") == 0

    def test_estimate_composition_cost_is_positive(self):
        assert _estimate_composition_cost_micros() > 0


# ── HTTP client behavior ────────────────────────────────────────────────


class TestSubmitRender:
    @pytest.mark.asyncio
    async def test_returns_job_on_success(self):
        async def handler(request):
            assert request.headers["authorization"] == "Bearer test"
            return httpx.Response(202, json={"job_id": "j1", "status": "queued"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _submit_render(
                client=client, base_url="http://x", bearer="test",
                payload={"request_id": "r"},
            )
        assert result["job_id"] == "j1"

    @pytest.mark.asyncio
    async def test_retries_on_network_error(self):
        attempts = {"n": 0}

        async def handler(request):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise httpx.ConnectError("boom")
            return httpx.Response(202, json={"job_id": "j", "status": "queued"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            # Speed up the test: monkeypatch the backoff to ~0
            from qanot.tools import video as vmod
            saved = vmod._SUBMIT_BACKOFF_BASE_S
            vmod._SUBMIT_BACKOFF_BASE_S = 0.01
            try:
                result = await _submit_render(
                    client=client, base_url="http://x", bearer="t",
                    payload={"request_id": "r"},
                )
            finally:
                vmod._SUBMIT_BACKOFF_BASE_S = saved
        assert result["job_id"] == "j"
        assert attempts["n"] == 2

    @pytest.mark.asyncio
    async def test_raises_after_retry_budget(self):
        async def handler(request):
            raise httpx.ConnectError("dead")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            from qanot.tools import video as vmod
            saved = vmod._SUBMIT_BACKOFF_BASE_S
            vmod._SUBMIT_BACKOFF_BASE_S = 0.01
            try:
                with pytest.raises(_ServiceUnavailable):
                    await _submit_render(
                        client=client, base_url="http://x", bearer="t",
                        payload={"request_id": "r"},
                    )
            finally:
                vmod._SUBMIT_BACKOFF_BASE_S = saved

    @pytest.mark.asyncio
    async def test_surfaces_4xx_as_http_error(self):
        async def handler(request):
            return httpx.Response(413, json={"error": "too_large"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await _submit_render(
                    client=client, base_url="http://x", bearer="t",
                    payload={"request_id": "r"},
                )


class TestPollJob:
    @pytest.mark.asyncio
    async def test_returns_terminal_status(self, monkeypatch):
        # Speed up polling
        from qanot.tools import video as vmod
        monkeypatch.setattr(vmod, "_POLL_INTERVAL_S", 0.01)

        states = iter([
            {"status": "rendering", "stage": "rendering_frames", "progress_percent": 30},
            {"status": "rendering", "stage": "rendering_frames", "progress_percent": 60},
            {"status": "succeeded", "stage": "succeeded", "progress_percent": 100,
             "output_path": "/p", "render_duration_seconds": 5},
        ])

        async def handler(request):
            return httpx.Response(200, json=next(states))

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _poll_job(
                client=client, base_url="http://x", bearer="t", job_id="j1",
            )
        assert result["status"] == "succeeded"


# ── Tool registration toggle ────────────────────────────────────────────


class TestRegistrationToggle:
    def test_engine_off_registers_nothing(self):
        from qanot.registry import ToolRegistry
        cfg = _StubConfig(video_engine="off")
        reg = ToolRegistry()
        register_video_tools(reg, config=cfg, workspace_dir="/tmp")
        assert "render_video" not in reg._handlers

    def test_engine_legacy_reels_registers_nothing(self):
        from qanot.registry import ToolRegistry
        cfg = _StubConfig(video_engine="legacy_reels")
        reg = ToolRegistry()
        register_video_tools(reg, config=cfg, workspace_dir="/tmp")
        assert "render_video" not in reg._handlers

    def test_engine_hyperframes_with_secret_registers(self):
        from qanot.registry import ToolRegistry
        cfg = _StubConfig(video_engine="hyperframes",
                          video_service_secret="s",
                          video_render_url="http://x")
        reg = ToolRegistry()
        register_video_tools(reg, config=cfg, workspace_dir="/tmp")
        assert "render_video" in reg._handlers

    def test_engine_hyperframes_missing_secret_registers_nothing(self):
        from qanot.registry import ToolRegistry
        cfg = _StubConfig(video_engine="hyperframes",
                          video_service_secret="",
                          video_render_url="http://x")
        reg = ToolRegistry()
        register_video_tools(reg, config=cfg, workspace_dir="/tmp")
        assert "render_video" not in reg._handlers

    def test_engine_hyperframes_missing_url_registers_nothing(self):
        from qanot.registry import ToolRegistry
        cfg = _StubConfig(video_engine="hyperframes",
                          video_service_secret="s",
                          video_render_url="")
        reg = ToolRegistry()
        register_video_tools(reg, config=cfg, workspace_dir="/tmp")
        assert "render_video" not in reg._handlers


# ── End-to-end render flow with mocked service ──────────────────────────


class TestRenderFlowEndToEnd:
    @pytest.mark.asyncio
    async def test_success_flow_pushes_pending_video(self, tmp_path, monkeypatch, _agent_singleton):
        # Speed everything up
        from qanot.tools import video as vmod
        monkeypatch.setattr(vmod, "_POLL_INTERVAL_S", 0.01)
        monkeypatch.setattr(vmod, "_SUBMIT_BACKOFF_BASE_S", 0.01)

        from qanot.registry import ToolRegistry
        cfg = _StubConfig(workspace_dir=str(tmp_path))
        reg = ToolRegistry()
        # Capture user_id for the lambda used by registration.
        user_box = {"u": "user42"}
        register_video_tools(
            reg, config=cfg, workspace_dir=str(tmp_path),
            get_user_id=lambda: user_box["u"],
        )

        # Build a mock httpx transport that handles all three endpoints.
        mp4_bytes = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200

        async def handler(request: httpx.Request):
            url = str(request.url)
            if url.endswith("/render"):
                body = json.loads(request.content)
                assert body["bot_id"] == cfg.bot_name
                assert body["user_id"] == user_box["u"]
                return httpx.Response(202, json={"job_id": "JOB1", "status": "queued"})
            if "/jobs/JOB1/output" in url:
                return httpx.Response(200, content=mp4_bytes,
                                       headers={"content-type": "video/mp4"})
            if "/jobs/JOB1" in url:
                return httpx.Response(200, json={
                    "status": "succeeded", "stage": "succeeded",
                    "progress_percent": 100,
                    "output_path": "/server/JOB1.mp4",
                    "render_duration_seconds": 8,
                })
            raise AssertionError(f"unexpected URL {url}")

        # Inject the mock transport into httpx.AsyncClient via monkeypatch.
        original_async_client = httpx.AsyncClient

        def _client_factory(*args, **kwargs):
            kwargs.pop("transport", None)
            return original_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _client_factory)

        handler_fn = reg._handlers["render_video"]
        result = json.loads(await handler_fn({"brief": "test brief", "duration": 5}))

        assert result["success"] is True
        assert result["render_seconds"] == 8
        # Pending-video queue should have an entry
        from qanot.agent import Agent
        videos = Agent._instance.pop_pending_videos("user42")
        assert len(videos) == 1
        assert videos[0].endswith("JOB1.mp4")

    @pytest.mark.asyncio
    async def test_lint_failed_no_retry_after_budget(self, tmp_path, monkeypatch, _agent_singleton):
        from qanot.tools import video as vmod
        monkeypatch.setattr(vmod, "_POLL_INTERVAL_S", 0.01)
        monkeypatch.setattr(vmod, "_SUBMIT_BACKOFF_BASE_S", 0.01)
        # Budget 1 -> first attempt + 1 retry = 2 attempts max
        assert _COMPOSITION_RETRY_BUDGET == 1

        from qanot.registry import ToolRegistry
        cfg = _StubConfig(workspace_dir=str(tmp_path))
        reg = ToolRegistry()
        register_video_tools(
            reg, config=cfg, workspace_dir=str(tmp_path),
            get_user_id=lambda: "u1",
        )

        attempts = {"n": 0}

        async def handler(request: httpx.Request):
            url = str(request.url)
            if url.endswith("/render"):
                attempts["n"] += 1
                return httpx.Response(202, json={"job_id": f"J{attempts['n']}", "status": "queued"})
            if "/jobs/" in url and "/output" not in url:
                return httpx.Response(200, json={
                    "status": "failed", "stage": "failed",
                    "error": {"code": "lint_failed", "message": "broken", "details": "data-duration missing"},
                })
            raise AssertionError(url)

        original_async_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda *a, **kw: original_async_client(
                *a, transport=httpx.MockTransport(handler),
                **{k: v for k, v in kw.items() if k != "transport"},
            ),
        )

        result = json.loads(await reg._handlers["render_video"]({"brief": "x"}))
        assert result["error"] == "lint_failed"
        assert attempts["n"] == _COMPOSITION_RETRY_BUDGET + 1  # initial + 1 retry

    @pytest.mark.asyncio
    async def test_user_rate_limit_blocks_after_quota(self, tmp_path, monkeypatch, _agent_singleton):
        from qanot.tools import video as vmod
        monkeypatch.setattr(vmod, "_POLL_INTERVAL_S", 0.01)

        from qanot.registry import ToolRegistry
        cfg = _StubConfig(
            workspace_dir=str(tmp_path),
            video_per_user_daily_limit=1,
        )
        reg = ToolRegistry()
        register_video_tools(
            reg, config=cfg, workspace_dir=str(tmp_path),
            get_user_id=lambda: "u1",
        )

        async def handler(request: httpx.Request):
            url = str(request.url)
            if url.endswith("/render"):
                return httpx.Response(202, json={"job_id": "J", "status": "queued"})
            if "/output" in url:
                return httpx.Response(200, content=b"\x00mp4", headers={"content-type": "video/mp4"})
            return httpx.Response(200, json={
                "status": "succeeded", "stage": "succeeded", "progress_percent": 100,
                "output_path": "/p", "render_duration_seconds": 5,
            })

        original = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda *a, **kw: original(
                *a, transport=httpx.MockTransport(handler),
                **{k: v for k, v in kw.items() if k != "transport"},
            ),
        )

        # First call succeeds + records
        first = json.loads(await reg._handlers["render_video"]({"brief": "1"}))
        assert first["success"] is True
        # Second call rate-limited
        second = json.loads(await reg._handlers["render_video"]({"brief": "2"}))
        assert second["error"] == "rate_limited"

    @pytest.mark.asyncio
    async def test_service_unavailable_returns_user_friendly_error(self, tmp_path, monkeypatch, _agent_singleton):
        from qanot.tools import video as vmod
        monkeypatch.setattr(vmod, "_POLL_INTERVAL_S", 0.01)
        monkeypatch.setattr(vmod, "_SUBMIT_BACKOFF_BASE_S", 0.001)

        from qanot.registry import ToolRegistry
        cfg = _StubConfig(workspace_dir=str(tmp_path))
        reg = ToolRegistry()
        register_video_tools(
            reg, config=cfg, workspace_dir=str(tmp_path),
            get_user_id=lambda: "u1",
        )

        async def handler(request):
            raise httpx.ConnectError("dead service")

        original = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda *a, **kw: original(
                *a, transport=httpx.MockTransport(handler),
                **{k: v for k, v in kw.items() if k != "transport"},
            ),
        )

        result = json.loads(await reg._handlers["render_video"]({"brief": "x"}))
        assert result["error"] == "service_unavailable"

    @pytest.mark.asyncio
    async def test_missing_brief_returns_validation_error(self, tmp_path, _agent_singleton):
        from qanot.registry import ToolRegistry
        cfg = _StubConfig(workspace_dir=str(tmp_path))
        reg = ToolRegistry()
        register_video_tools(
            reg, config=cfg, workspace_dir=str(tmp_path),
            get_user_id=lambda: "u1",
        )
        result = json.loads(await reg._handlers["render_video"]({}))
        assert result["error"] == "missing_brief"
