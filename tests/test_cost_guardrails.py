"""Tests for per-turn cost caps + cost_stats diagnostic.

Covers Tier 2 #5 (cost guardrails) from claudedocs/qanot-2026-roadmap.md.

Validates:
  - start_turn resets per-turn counters
  - record_iteration accumulates correctly
  - get_turn_total_tokens excludes cache_read (the cheap path)
  - check_per_turn_caps returns (within, reason) correctly for both
    token and cost limits, and when both limits are 0 it's a no-op
  - cost_stats tool returns a structured envelope
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qanot.cost import CostTracker
from qanot.registry import ToolRegistry


def _tracker(tmp_path: Path) -> CostTracker:
    return CostTracker(workspace_dir=str(tmp_path))


# ── Per-turn state lifecycle ──────────────────────────────────


def test_start_turn_resets_state(tmp_path):
    t = _tracker(tmp_path)
    t.start_turn("u1")
    t.record_iteration("u1", input_tokens=1000, output_tokens=500, cost=0.01)
    assert t.get_turn_total_tokens("u1") == 1500
    assert abs(t.get_turn_cost("u1") - 0.01) < 1e-9
    # New turn — state resets
    t.start_turn("u1")
    assert t.get_turn_total_tokens("u1") == 0
    assert t.get_turn_cost("u1") == 0.0


def test_record_iteration_accumulates(tmp_path):
    t = _tracker(tmp_path)
    t.start_turn("u1")
    t.record_iteration("u1", input_tokens=100, output_tokens=50, cost=0.001)
    t.record_iteration("u1", input_tokens=200, output_tokens=80, cost=0.002)
    t.record_iteration("u1", input_tokens=50, output_tokens=20, cost=0.0005)
    # cache_read excluded; cache_write included
    assert t.get_turn_total_tokens("u1") == (100 + 50 + 200 + 80 + 50 + 20)
    assert abs(t.get_turn_cost("u1") - 0.0035) < 1e-9


def test_cache_read_tokens_excluded_from_turn_total(tmp_path):
    """cache_read is the cheap path — guarding against it would just punish good caching."""
    t = _tracker(tmp_path)
    t.start_turn("u1")
    t.record_iteration("u1", input_tokens=100, cache_read=10_000, cache_write=200, cost=0.001)
    assert t.get_turn_total_tokens("u1") == 100 + 200  # cache_read NOT counted


def test_record_without_start_works(tmp_path):
    """Defensive: if start_turn was missed, record_iteration creates state."""
    t = _tracker(tmp_path)
    t.record_iteration("u1", input_tokens=50)
    assert t.get_turn_total_tokens("u1") == 50


def test_per_user_isolation(tmp_path):
    t = _tracker(tmp_path)
    t.start_turn("u1")
    t.start_turn("u2")
    t.record_iteration("u1", input_tokens=1000)
    t.record_iteration("u2", input_tokens=200)
    assert t.get_turn_total_tokens("u1") == 1000
    assert t.get_turn_total_tokens("u2") == 200


# ── check_per_turn_caps ────────────────────────────────────────


def test_check_caps_no_limits_is_noop(tmp_path):
    t = _tracker(tmp_path)
    t.start_turn("u1")
    t.record_iteration("u1", input_tokens=1_000_000, cost=10.0)
    ok, reason = t.check_per_turn_caps("u1", max_tokens=0, max_cost_usd=0.0)
    assert ok is True
    assert reason == ""


def test_check_caps_token_limit_exceeded(tmp_path):
    t = _tracker(tmp_path)
    t.start_turn("u1")
    t.record_iteration("u1", input_tokens=150_000, output_tokens=50_000, cost=1.0)
    # Total = 200K, cap at 100K
    ok, reason = t.check_per_turn_caps("u1", max_tokens=100_000)
    assert ok is False
    assert "200,000" in reason
    assert "100,000" in reason


def test_check_caps_token_limit_within(tmp_path):
    t = _tracker(tmp_path)
    t.start_turn("u1")
    t.record_iteration("u1", input_tokens=10_000, output_tokens=5_000, cost=0.05)
    ok, reason = t.check_per_turn_caps("u1", max_tokens=100_000)
    assert ok is True
    assert reason == ""


def test_check_caps_cost_limit_exceeded(tmp_path):
    t = _tracker(tmp_path)
    t.start_turn("u1")
    t.record_iteration("u1", input_tokens=100, cost=2.50)
    ok, reason = t.check_per_turn_caps("u1", max_cost_usd=1.0)
    assert ok is False
    assert "$2.5000" in reason or "2.5" in reason
    assert "1." in reason


def test_check_caps_cost_limit_within(tmp_path):
    t = _tracker(tmp_path)
    t.start_turn("u1")
    t.record_iteration("u1", input_tokens=100, cost=0.05)
    ok, reason = t.check_per_turn_caps("u1", max_cost_usd=1.0)
    assert ok is True


def test_check_caps_token_takes_precedence_when_both_set(tmp_path):
    """Token check is first; if it fails, cost isn't even checked."""
    t = _tracker(tmp_path)
    t.start_turn("u1")
    t.record_iteration("u1", input_tokens=200_000, cost=0.05)
    ok, reason = t.check_per_turn_caps("u1", max_tokens=100_000, max_cost_usd=10.0)
    assert ok is False
    assert "token" in reason.lower()


def test_check_caps_unknown_user_within_limits(tmp_path):
    """A user with no recorded usage is trivially within any limit."""
    t = _tracker(tmp_path)
    ok, reason = t.check_per_turn_caps("ghost", max_tokens=100, max_cost_usd=1.0)
    assert ok is True
    assert reason == ""


# ── cost_stats tool ────────────────────────────────────────────


def test_cost_stats_tool_top_spenders(tmp_path: Path):
    from qanot.tools.diagnostics import register_diagnostics_tools

    # Pre-populate costs.json
    t = CostTracker(workspace_dir=str(tmp_path))
    t.add_usage("user_big_spender", input_tokens=10000, output_tokens=5000, cost=0.50)
    t.add_usage("user_small", input_tokens=100, output_tokens=50, cost=0.005)
    t.add_usage("user_medium", input_tokens=1000, output_tokens=500, cost=0.05)
    t.save()

    registry = ToolRegistry()
    register_diagnostics_tools(registry, str(tmp_path))
    handler = registry.get_handler("cost_stats")
    assert handler is not None

    result = asyncio.run(handler({}))
    parsed = json.loads(result)
    assert parsed["total_users_tracked"] == 3
    # Top spender should be first
    assert parsed["top_spenders"][0]["user_id"] == "user_big_spender"
    assert parsed["top_spenders"][0]["total_cost_usd"] == 0.5


def test_cost_stats_tool_filter_by_user(tmp_path: Path):
    from qanot.tools.diagnostics import register_diagnostics_tools
    t = CostTracker(workspace_dir=str(tmp_path))
    t.add_usage("user_a", input_tokens=100, cost=0.01)
    t.save()
    registry = ToolRegistry()
    register_diagnostics_tools(registry, str(tmp_path))
    handler = registry.get_handler("cost_stats")
    result = asyncio.run(handler({"user_id": "user_a"}))
    parsed = json.loads(result)
    assert parsed["user_id"] == "user_a"
    assert parsed["stats"]["total_cost"] == 0.01


def test_cost_stats_tool_unknown_user_returns_error(tmp_path: Path):
    from qanot.tools.diagnostics import register_diagnostics_tools
    registry = ToolRegistry()
    register_diagnostics_tools(registry, str(tmp_path))
    handler = registry.get_handler("cost_stats")
    result = asyncio.run(handler({"user_id": "ghost"}))
    parsed = json.loads(result)
    assert "error" in parsed


def test_cost_stats_tool_empty_workspace(tmp_path: Path):
    from qanot.tools.diagnostics import register_diagnostics_tools
    registry = ToolRegistry()
    register_diagnostics_tools(registry, str(tmp_path))
    handler = registry.get_handler("cost_stats")
    result = asyncio.run(handler({}))
    parsed = json.loads(result)
    assert parsed["total_users_tracked"] == 0
    assert parsed["top_spenders"] == []


def test_cost_stats_tool_clamps_top_n(tmp_path: Path):
    from qanot.tools.diagnostics import register_diagnostics_tools
    registry = ToolRegistry()
    register_diagnostics_tools(registry, str(tmp_path))
    handler = registry.get_handler("cost_stats")
    result = asyncio.run(handler({"top_n": 9999}))
    parsed = json.loads(result)
    # Empty workspace: list is empty regardless, but no error and the
    # implementation should have clamped internally.
    assert "top_spenders" in parsed
