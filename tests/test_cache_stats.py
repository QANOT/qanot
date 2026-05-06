"""Tests for cache_stats aggregation + dynamic suffix budget enforcement.

Covers Hermes-borrow item #4 (cache-aware budget discipline):
  - aggregate_session_files reads JSONL, sums usage, computes ratios
  - format_health_assessment maps ratio to plain-English verdict
  - cache_stats tool envelope correct
  - Dynamic suffix is bounded by MAX_DYNAMIC_SUFFIX_CHARS
  - Cache-stable prefix doctrine present in system prompt
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from qanot.cache_stats import (
    CacheUsageSummary,
    aggregate_session_files,
    format_health_assessment,
)
from qanot.registry import ToolRegistry


# ── aggregate_session_files ────────────────────────────────────


def _write_session(path: Path, entries: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_aggregate_empty_dir(tmp_path: Path):
    summary = aggregate_session_files(tmp_path / "empty")
    assert summary.turns == 0
    assert summary.cache_read_ratio == 0.0


def test_aggregate_sums_usage_across_files(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_session(sessions / "a.jsonl", [
        {"role": "assistant", "usage": {"input": 100, "output": 50,
                                         "cacheRead": 5000, "cacheWrite": 0,
                                         "cost": {"total": 0.01}}},
        {"role": "assistant", "usage": {"input": 80, "output": 40,
                                         "cacheRead": 4900, "cacheWrite": 100,
                                         "cost": {"total": 0.012}}},
    ])
    _write_session(sessions / "b.jsonl", [
        {"role": "assistant", "usage": {"input": 200, "output": 100,
                                         "cacheRead": 6000, "cacheWrite": 0,
                                         "cost": {"total": 0.02}}},
    ])
    summary = aggregate_session_files(sessions)
    assert summary.turns == 3
    assert summary.total_input_tokens == 380
    assert summary.total_cache_read_tokens == 15900
    assert summary.total_cache_write_tokens == 100
    assert summary.total_output_tokens == 190
    assert abs(summary.total_cost_usd - 0.042) < 1e-6


def test_aggregate_skips_old_files(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    old = sessions / "old.jsonl"
    new = sessions / "new.jsonl"
    _write_session(old, [{"usage": {"input": 999, "cacheRead": 0, "cacheWrite": 0, "output": 0}}])
    _write_session(new, [{"usage": {"input": 100, "cacheRead": 0, "cacheWrite": 0, "output": 0}}])
    # Make old file's mtime 30 days ago
    old_ts = time.time() - (30 * 86400)
    import os
    os.utime(old, (old_ts, old_ts))
    summary = aggregate_session_files(sessions, days=7)
    assert summary.total_input_tokens == 100  # only the new file


def test_aggregate_tolerates_corrupt_lines(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    f = sessions / "mixed.jsonl"
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"usage": {"input": 50, "cacheRead": 100, "cacheWrite": 0, "output": 25}}) + "\n")
        fh.write('{"incomplete":\n')
    summary = aggregate_session_files(sessions)
    assert summary.turns == 1


def test_aggregate_skips_entries_without_usage(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_session(sessions / "x.jsonl", [
        {"role": "user", "text": "hi"},  # no usage
        {"role": "assistant", "usage": {"input": 10, "cacheRead": 100, "cacheWrite": 0, "output": 5}},
        {"role": "system", "note": "foo"},  # no usage
    ])
    summary = aggregate_session_files(sessions)
    assert summary.turns == 1


def test_cache_read_ratio_calculation():
    # 1000 cache_read + 100 input + 0 cache_write → ratio = 1000 / 1100 = 0.909
    s = CacheUsageSummary(turns=1, total_input_tokens=100, total_cache_read_tokens=1000,
                          total_cache_write_tokens=0, total_output_tokens=50, total_cost_usd=0.01)
    assert abs(s.cache_read_ratio - 0.9091) < 0.001


def test_cache_read_ratio_zero_when_no_data():
    s = CacheUsageSummary(turns=0, total_input_tokens=0, total_cache_read_tokens=0,
                          total_cache_write_tokens=0, total_output_tokens=0, total_cost_usd=0)
    assert s.cache_read_ratio == 0.0


# ── format_health_assessment ───────────────────────────────────


def test_health_excellent():
    s = CacheUsageSummary(turns=10, total_input_tokens=100, total_cache_read_tokens=10_000,
                          total_cache_write_tokens=0, total_output_tokens=500, total_cost_usd=0.1)
    assert "excellent" in format_health_assessment(s).lower()


def test_health_degraded():
    # ratio ~0.6
    s = CacheUsageSummary(turns=10, total_input_tokens=400, total_cache_read_tokens=600,
                          total_cache_write_tokens=0, total_output_tokens=500, total_cost_usd=0.1)
    assert "degraded" in format_health_assessment(s).lower()


def test_health_poor():
    # ratio ~0.3
    s = CacheUsageSummary(turns=10, total_input_tokens=700, total_cache_read_tokens=300,
                          total_cache_write_tokens=0, total_output_tokens=500, total_cost_usd=0.1)
    assert "poor" in format_health_assessment(s).lower()


def test_health_no_data():
    s = CacheUsageSummary(0, 0, 0, 0, 0, 0)
    assert "no recent" in format_health_assessment(s).lower()


# ── cache_stats tool ───────────────────────────────────────────


def test_cache_stats_tool_envelope(tmp_path: Path):
    from qanot.tools.diagnostics import register_diagnostics_tools
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_session(sessions / "x.jsonl", [
        {"usage": {"input": 50, "cacheRead": 5000, "cacheWrite": 0, "output": 25,
                   "cost": {"total": 0.001}}},
    ])
    registry = ToolRegistry()
    register_diagnostics_tools(registry, str(tmp_path), sessions_dir=str(sessions))
    handler = registry.get_handler("cache_stats")
    assert handler is not None
    result = asyncio.run(handler({"days": 7}))
    parsed = json.loads(result)
    assert parsed["window_days"] == 7
    assert parsed["summary"]["turns"] == 1
    assert parsed["summary"]["cache_read_ratio"] > 0.95
    assert "excellent" in parsed["health"].lower() or "healthy" in parsed["health"].lower()


def test_cache_stats_tool_clamps_days(tmp_path: Path):
    from qanot.tools.diagnostics import register_diagnostics_tools
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    registry = ToolRegistry()
    register_diagnostics_tools(registry, str(tmp_path), sessions_dir=str(sessions))
    handler = registry.get_handler("cache_stats")
    result = asyncio.run(handler({"days": 999}))
    parsed = json.loads(result)
    assert parsed["window_days"] == 90  # clamped


# ── dynamic suffix budget enforcement ──────────────────────────


def test_dynamic_suffix_capped_when_memory_md_huge(tmp_path: Path):
    """Build a system prompt with a 50KB MEMORY.md and verify the
    post-boundary content is capped at the dynamic budget."""
    from qanot.prompt import (
        MAX_DYNAMIC_SUFFIX_CHARS,
        _CACHE_BOUNDARY,
        build_system_prompt,
    )
    huge_memory = "important fact\n" * 5000  # ~70KB
    (tmp_path / "MEMORY.md").write_text(huge_memory, encoding="utf-8")

    prompt = build_system_prompt(workspace_dir=str(tmp_path), mode="full")
    assert _CACHE_BOUNDARY in prompt
    _, dynamic = prompt.split(_CACHE_BOUNDARY, 1)
    # The dynamic suffix (everything after the boundary) must respect
    # the cap. Allow ~2KB headroom for session-info appended after.
    assert len(dynamic) <= MAX_DYNAMIC_SUFFIX_CHARS + 2_000, (
        f"dynamic suffix is {len(dynamic)} chars; cap is "
        f"{MAX_DYNAMIC_SUFFIX_CHARS}"
    )


def test_dynamic_suffix_below_cap_when_inputs_small(tmp_path: Path):
    """Sanity: a small workspace produces a small dynamic suffix
    (we don't accidentally pad up to the cap)."""
    from qanot.prompt import _CACHE_BOUNDARY, build_system_prompt
    (tmp_path / "MEMORY.md").write_text("tiny fact\n", encoding="utf-8")
    prompt = build_system_prompt(workspace_dir=str(tmp_path), mode="full")
    _, dynamic = prompt.split(_CACHE_BOUNDARY, 1)
    assert len(dynamic) < 5_000  # well under the cap


def test_cache_stable_prefix_doctrine_in_prompt(tmp_path: Path):
    """The doctrinal rule about not mutating stable prefix mid-turn
    should appear in the hardcoded behavioral block."""
    from qanot.prompt import build_system_prompt
    prompt = build_system_prompt(workspace_dir=str(tmp_path), mode="full")
    assert "Cache-Stable Prefix" in prompt
    # Key files we don't want the agent editing mid-conversation
    for f in ("SOUL.md", "USER.md", "TOOLS.md"):
        assert f in prompt
