"""Tests for OAuth quota reporting (qanot/usage_quota.py).

Two sources: the /api/oauth/usage endpoint (subscription windows) and
anthropic-ratelimit-* response headers (API-key accounts).
"""

from __future__ import annotations

import time

import pytest

import qanot.usage_quota as uq


def _reset():
    uq._LATEST_HEADERS = None
    uq._FETCH_CACHE = (0.0, None)


# ── header source (API-key accounts) ──────────────────────────────────

def test_parses_generic_ratelimit_headers():
    _reset()
    uq.update_from_headers({
        "anthropic-ratelimit-requests-limit": "1000",
        "anthropic-ratelimit-requests-remaining": "380",
        "anthropic-ratelimit-unified-5h-limit": "100",
        "anthropic-ratelimit-unified-5h-remaining": "38",
        "content-type": "application/json",  # ignored
    })
    snap = uq.latest_headers()
    assert snap is not None
    assert set(snap.windows) == {"requests", "unified-5h"}
    assert snap.windows["requests"].remaining_pct == 38
    assert snap.windows["unified-5h"].remaining == 38


def test_retry_after_and_reset_seconds():
    _reset()
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    uq.update_from_headers({
        "anthropic-ratelimit-requests-limit": "100",
        "anthropic-ratelimit-requests-remaining": "0",
        "anthropic-ratelimit-requests-reset": future,
        "retry-after": "120",
    })
    snap = uq.latest_headers()
    assert snap.retry_after == 120
    assert 3500 <= snap.windows["requests"].reset_in_seconds <= 3600


def test_malformed_headers_never_raise():
    _reset()
    uq.update_from_headers(None)
    uq.update_from_headers("garbage")
    uq.update_from_headers({"anthropic-ratelimit-requests-remaining": "nope"})
    snap = uq.latest_headers()
    assert snap is not None and snap.windows["requests"].remaining is None


def test_no_headers_no_snapshot():
    _reset()
    uq.update_from_headers({"content-type": "application/json"})
    assert uq.latest_headers() is None


# ── OAuth usage endpoint ──────────────────────────────────────────────

def test_parse_usage_payload_windows_and_extra():
    snap = uq._parse_usage_payload({
        "five_hour": {"utilization": 0.62, "resets_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1800))},
        "seven_day": {"utilization": 28},  # already a percentage
        "extra_usage": {"is_enabled": True, "used_credits": 1.2,
                        "monthly_limit": 5.0, "currency": "USD"},
    }, time.time())
    assert snap.source == "oauth_usage_api"
    assert round(snap.windows["five_hour"].used_pct) == 62
    assert snap.windows["five_hour"].remaining_pct == 38
    assert round(snap.windows["seven_day"].used_pct) == 28
    assert "1.20 / 5.00 USD" in snap.extra_usage
    report = uq.format_report(snap)
    assert "Sessiya (5 soat): 62% ishlatilgan" in report
    assert "Extra usage" in report


@pytest.mark.asyncio
async def test_fetch_oauth_usage_non_oauth_token_returns_none():
    _reset()
    assert await uq.fetch_oauth_usage("sk-ant-api-xxx") is None
    assert await uq.fetch_oauth_usage("") is None


@pytest.mark.asyncio
async def test_fetch_oauth_usage_403_scope(monkeypatch):
    _reset()

    class _Resp:
        status_code = 403
        def json(self): return {"error": {"message": "scope"}}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    snap = await uq.fetch_oauth_usage("sk-ant-oat01-xxx")
    assert snap is not None and snap.windows == {}
    assert "user:profile" in snap.unavailable_reason
    assert "scope yetishmaydi" not in uq.format_report(snap)  # uses the real reason
    assert "OAuth limit" in uq.format_report(snap)


@pytest.mark.asyncio
async def test_build_usage_report_prefers_endpoint(monkeypatch):
    _reset()

    class _Resp:
        status_code = 200
        def json(self):
            return {"five_hour": {"utilization": 0.1}}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    report = await uq.build_usage_report("sk-ant-oat01-xxx")
    assert "Sessiya (5 soat): 10% ishlatilgan" in report


# ── chat() hot-path wiring: with_raw_response + header capture ──

def _fake_message():
    block = type("Block", (), {"type": "text", "text": "hi", "input": {}, "id": "", "name": ""})()
    usage = type("U", (), {
        "input_tokens": 10, "output_tokens": 3,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    })()
    return type("Msg", (), {"content": [block], "stop_reason": "end_turn", "usage": usage})()


@pytest.mark.asyncio
async def test_chat_uses_raw_response_and_captures_headers():
    _reset()
    from qanot.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key="sk-ant-api-test", model="claude-sonnet-4-6")
    captured = {}

    class _RawResp:
        headers = {
            "anthropic-ratelimit-unified-5h-limit": "100",
            "anthropic-ratelimit-unified-5h-remaining": "61",
        }
        def parse(self): return _fake_message()

    async def _create(**kwargs):
        captured["called"] = True
        return _RawResp()

    provider.client = type("C", (), {})()
    provider.client.messages = type("M", (), {})()
    provider.client.messages.with_raw_response = type("R", (), {})()
    provider.client.messages.with_raw_response.create = _create

    resp = await provider.chat(messages=[{"role": "user", "content": "hi"}])
    assert captured.get("called") is True
    assert resp.content == "hi" and resp.usage.input_tokens == 10
    snap = uq.latest_headers()
    assert snap is not None and snap.windows["unified-5h"].remaining == 61
