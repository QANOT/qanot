"""Tests for the quick-wins batch: skill-usage wiring (B1) + Retry-After (B2)."""

from __future__ import annotations

import types

import pytest


# ── B1: skill activation records usage ────────────────────────────────

def test_record_skill_uses_bumps_usage(tmp_path):
    """Activating skills must bump use_count/last_used_at so the curator's
    freshness anchor is actual use, not creation date."""
    from qanot.agent.preprocessing import _PreprocessingMixin
    from qanot.skills.usage import UsageStore

    skills_root = tmp_path / "skills"
    skills_root.mkdir()

    # Minimal stand-in carrying just .config.workspace_dir.
    fake = types.SimpleNamespace(
        config=types.SimpleNamespace(workspace_dir=str(tmp_path)),
    )
    # Matched items expose .spec.name (LoadedSkill) — emulate that shape.
    matched = [
        types.SimpleNamespace(spec=types.SimpleNamespace(name="bambuk-pricer")),
        types.SimpleNamespace(spec=types.SimpleNamespace(name="report-writer")),
    ]

    # No running loop here → helper writes inline.
    _PreprocessingMixin._record_skill_uses(fake, matched)

    recs = UsageStore(skills_root).load()
    assert recs["bambuk-pricer"].use_count == 1
    assert recs["bambuk-pricer"].last_used_at != ""
    assert recs["report-writer"].use_count == 1

    # A second activation increments, not resets.
    _PreprocessingMixin._record_skill_uses(fake, matched[:1])
    recs = UsageStore(skills_root).load()
    assert recs["bambuk-pricer"].use_count == 2


def test_record_skill_uses_handles_bare_spec_and_empty(tmp_path):
    from qanot.agent.preprocessing import _PreprocessingMixin

    (tmp_path / "skills").mkdir()
    fake = types.SimpleNamespace(
        config=types.SimpleNamespace(workspace_dir=str(tmp_path)),
    )
    # item with no .spec (a bare SkillSpec) + an item with no name → skipped
    bare = types.SimpleNamespace(name="plain")
    nameless = types.SimpleNamespace(spec=types.SimpleNamespace(name=None))
    _PreprocessingMixin._record_skill_uses(fake, [bare, nameless])

    from qanot.skills.usage import UsageStore
    recs = UsageStore(tmp_path / "skills").load()
    assert "plain" in recs and recs["plain"].use_count == 1
    assert None not in recs

    # empty list is a no-op (no crash)
    _PreprocessingMixin._record_skill_uses(fake, [])


# ── B2: Retry-After extraction ────────────────────────────────────────

def test_retry_after_seconds_extracts_header():
    from qanot.agent.loop import _retry_after_seconds

    exc = types.SimpleNamespace(
        response=types.SimpleNamespace(headers={"retry-after": "30"})
    )
    assert _retry_after_seconds(exc) == 30.0


def test_retry_after_seconds_none_when_absent_or_malformed():
    from qanot.agent.loop import _retry_after_seconds

    assert _retry_after_seconds(Exception("boom")) is None  # no .response
    no_hdr = types.SimpleNamespace(response=types.SimpleNamespace(headers={}))
    assert _retry_after_seconds(no_hdr) is None
    http_date = types.SimpleNamespace(
        response=types.SimpleNamespace(headers={"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"})
    )
    assert _retry_after_seconds(http_date) is None  # non-numeric → fall back to backoff


@pytest.mark.asyncio
async def test_retry_loop_honors_retry_after(monkeypatch):
    """When the provider 429s with Retry-After, the loop waits ~that long,
    not the blind exponential 1s."""
    import qanot.agent.loop as loop_mod
    from qanot.providers.errors import ERROR_RATE_LIMIT

    slept: list[float] = []

    async def _fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(loop_mod.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(loop_mod, "classify_error", lambda e: ERROR_RATE_LIMIT)

    calls = {"n": 0}

    class _RateLimited(Exception):
        response = types.SimpleNamespace(headers={"retry-after": "25"})

    class _Provider:
        async def chat(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _RateLimited("429")
            from qanot.providers.base import ProviderResponse
            return ProviderResponse(content="ok")

    # Minimal host object exposing .provider + the method under test.
    host = types.SimpleNamespace(provider=_Provider())
    resp = await loop_mod._LoopMixin._call_provider_with_retry(
        host, messages=[], tools=None, system="",
    )
    assert resp.content == "ok"
    # Waited ~25s (server value), not the blind 1s exponential base.
    assert slept and 25.0 <= slept[0] <= 26.0
