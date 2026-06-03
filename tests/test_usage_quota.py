"""Tests for OAuth rate-limit / quota header capture (qanot/usage_quota.py)."""

from __future__ import annotations

import time

import qanot.usage_quota as uq


def _reset():
    uq._LATEST = None


def test_parses_generic_ratelimit_headers():
    _reset()
    uq.update_from_headers({
        "anthropic-ratelimit-requests-limit": "1000",
        "anthropic-ratelimit-requests-remaining": "380",
        "anthropic-ratelimit-tokens-limit": "2000000",
        "anthropic-ratelimit-tokens-remaining": "740000",
        "anthropic-ratelimit-unified-5h-limit": "100",
        "anthropic-ratelimit-unified-5h-remaining": "38",
        "content-type": "application/json",  # ignored
    })
    snap = uq.latest()
    assert snap is not None
    assert set(snap.windows) == {"requests", "tokens", "unified-5h"}
    assert snap.windows["requests"].remaining == 380
    assert snap.windows["requests"].limit == 1000
    assert snap.windows["requests"].remaining_pct == 38
    # multi-segment window name preserved
    assert snap.windows["unified-5h"].remaining == 38


def test_reset_in_seconds_and_retry_after():
    _reset()
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    uq.update_from_headers({
        "anthropic-ratelimit-requests-limit": "100",
        "anthropic-ratelimit-requests-remaining": "0",
        "anthropic-ratelimit-requests-reset": future,
        "retry-after": "120",
    })
    snap = uq.latest()
    assert snap.retry_after == 120
    secs = snap.windows["requests"].reset_in_seconds
    assert 3500 <= secs <= 3600
    assert snap.windows["requests"].remaining_pct == 0


def test_format_report_renders_uzbek():
    _reset()
    uq.update_from_headers({
        "anthropic-ratelimit-unified-5h-limit": "100",
        "anthropic-ratelimit-unified-5h-remaining": "38",
    })
    report = uq.format_report()
    assert "OAuth limit" in report
    assert "unified-5h" in report
    assert "38" in report and "%" in report


def test_no_headers_no_snapshot():
    _reset()
    uq.update_from_headers({"content-type": "application/json"})
    assert uq.latest() is None
    assert uq.format_report() == ""


def test_malformed_headers_never_raise():
    _reset()
    uq.update_from_headers(None)            # not a mapping
    uq.update_from_headers("garbage")        # no .items()
    uq.update_from_headers({"anthropic-ratelimit-requests-remaining": "not-an-int"})
    # remaining unpar,seable → stays None, no crash
    snap = uq.latest()
    assert snap is not None
    assert snap.windows["requests"].remaining is None


def test_preferred_ordering_unified_first():
    _reset()
    uq.update_from_headers({
        "anthropic-ratelimit-tokens-remaining": "5",
        "anthropic-ratelimit-tokens-limit": "10",
        "anthropic-ratelimit-unified-remaining": "9",
        "anthropic-ratelimit-unified-limit": "10",
    })
    report = uq.format_report()
    assert report.index("unified") < report.index("tokens")


# ── chat() hot-path wiring: with_raw_response + quota capture ──

import pytest


def _fake_message():
    """Minimal stand-in for an anthropic Message."""
    block = type("Block", (), {"type": "text", "text": "hi", "input": {}, "id": "", "name": ""})()
    usage = type("U", (), {
        "input_tokens": 10, "output_tokens": 3,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    })()
    return type("Msg", (), {
        "content": [block], "stop_reason": "end_turn", "usage": usage,
    })()


@pytest.mark.asyncio
async def test_chat_uses_raw_response_and_captures_quota(monkeypatch):
    _reset()
    from qanot.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key="sk-ant-api-test", model="claude-sonnet-4-6")

    captured = {}

    class _RawResp:
        headers = {
            "anthropic-ratelimit-unified-5h-limit": "100",
            "anthropic-ratelimit-unified-5h-remaining": "61",
        }

        def parse(self):
            return _fake_message()

    async def _create(**kwargs):
        captured["called"] = True
        return _RawResp()

    # Replace the SDK client's raw-response create with our stub.
    provider.client = type("C", (), {})()
    provider.client.messages = type("M", (), {})()
    provider.client.messages.with_raw_response = type("R", (), {})()
    provider.client.messages.with_raw_response.create = _create

    resp = await provider.chat(messages=[{"role": "user", "content": "hi"}])

    assert captured.get("called") is True          # went through with_raw_response
    assert resp.content == "hi"                      # .parse() result used
    assert resp.usage.input_tokens == 10
    snap = uq.latest()
    assert snap is not None and snap.windows["unified-5h"].remaining == 61
