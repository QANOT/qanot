"""Tests for the Tool Search Tool / defer_loading wiring.

Validates that:
  - When tool_search_enabled=False, the tools list passes through unchanged
  - When True, non-eager tools get defer_loading=True
  - Eager tools (matched by prefix) stay unmodified
  - tool_search_tool_bm25 is appended to the array
  - Anthropic server tools (type-only, no name) are left as eager
  - Empty/None inputs are handled cleanly
"""

from __future__ import annotations

from qanot.providers.anthropic import AnthropicProvider


def _provider(enabled: bool, prefixes=None) -> AnthropicProvider:
    """Build a provider without hitting any API."""
    return AnthropicProvider(
        api_key="sk-ant-test-key",
        tool_search_enabled=enabled,
        eager_tool_prefixes=prefixes,
    )


# ── Disabled ───────────────────────────────────────────────────


def test_disabled_passes_tools_through_unchanged():
    p = _provider(False, ["read_", "web_"])
    tools = [
        {"name": "read_file", "description": "x", "input_schema": {}},
        {"name": "topkey_list_tasks", "description": "y", "input_schema": {}},
    ]
    out = p._apply_tool_search(tools)
    assert out == tools


def test_disabled_returns_none_for_none():
    p = _provider(False)
    assert p._apply_tool_search(None) is None


# ── Eager prefix matching ──────────────────────────────────────


def test_exact_name_match_is_eager():
    p = _provider(True, ["evolve_soul"])
    assert p._is_eager_tool("evolve_soul") is True
    assert p._is_eager_tool("evolve_soul_extra") is True  # also matches as prefix
    assert p._is_eager_tool("topkey_list_tasks") is False


def test_prefix_match_is_eager():
    p = _provider(True, ["read_", "write_"])
    assert p._is_eager_tool("read_file") is True
    assert p._is_eager_tool("read_dir") is True
    assert p._is_eager_tool("write_file") is True
    assert p._is_eager_tool("topkey_list_tasks") is False
    assert p._is_eager_tool("absmarket_query") is False


def test_empty_prefixes_means_all_eager():
    """Defensive: if no prefixes configured, treat all as eager (no defer)."""
    p = _provider(True, [])
    assert p._is_eager_tool("anything_at_all") is True


# ── Tool list transformation ──────────────────────────────────


def test_enabled_marks_non_eager_tools_with_defer_loading():
    p = _provider(True, ["read_", "web_search"])
    tools = [
        {"name": "read_file", "description": "x", "input_schema": {}},
        {"name": "web_search", "description": "y", "input_schema": {}},
        {"name": "topkey_list_tasks", "description": "z", "input_schema": {}},
        {"name": "absmarket_query", "description": "q", "input_schema": {}},
    ]
    out = p._apply_tool_search(tools)
    assert out is not None
    by_name = {t.get("name"): t for t in out if "name" in t}
    # Eager: no defer_loading
    assert "defer_loading" not in by_name["read_file"]
    assert "defer_loading" not in by_name["web_search"]
    # Non-eager: defer_loading=True
    assert by_name["topkey_list_tasks"]["defer_loading"] is True
    assert by_name["absmarket_query"]["defer_loading"] is True


def test_enabled_appends_tool_search_tool():
    p = _provider(True, ["read_"])
    out = p._apply_tool_search([
        {"name": "read_file", "description": "x", "input_schema": {}},
    ])
    assert out is not None
    # Last entry should be the bm25 server tool
    last = out[-1]
    assert last.get("type") == "tool_search_tool_bm25_20251119"
    # Anthropic requires the name to match the type stem exactly —
    # `tool_search_tool_bm25` (no version suffix). Other values get a
    # 400 invalid_request from the API.
    assert last.get("name") == "tool_search_tool_bm25"


def test_enabled_preserves_original_tool_count_plus_search():
    p = _provider(True, ["read_"])
    in_tools = [{"name": f"tool_{i}", "input_schema": {}} for i in range(10)]
    out = p._apply_tool_search(in_tools)
    assert out is not None
    assert len(out) == 11  # original 10 + 1 server tool


def test_enabled_skips_anthropic_server_tools():
    """code_execution / memory_20250818 are type-only, no `name`. They
    should pass through eager regardless of prefix list."""
    p = _provider(True, ["read_"])
    tools = [
        {"name": "read_file", "description": "x", "input_schema": {}},
        {"type": "code_execution_20250825"},  # type-only, no name
        {"type": "memory_20250818", "name": "memory"},  # both type and name
    ]
    out = p._apply_tool_search(tools)
    assert out is not None
    # All three input tools preserved, plus the bm25 search tool
    assert len(out) == 4
    # The type-only tool kept as-is (no defer_loading)
    server_tool = next(t for t in out if t.get("type") == "code_execution_20250825")
    assert "defer_loading" not in server_tool


def test_enabled_does_not_mutate_input_list():
    """Defensive: caller's tools list must not be mutated in place."""
    p = _provider(True, ["read_"])
    in_tools = [
        {"name": "read_file", "input_schema": {}},
        {"name": "topkey_list", "input_schema": {}},
    ]
    snapshot = [dict(t) for t in in_tools]
    out = p._apply_tool_search(in_tools)
    assert in_tools == snapshot, "input list was mutated"
    assert out is not None
    assert len(out) == 3  # 2 input + 1 bm25


def test_enabled_with_empty_tools_returns_input():
    """Don't add tool_search to an empty tools array — it'd be useless overhead."""
    p = _provider(True, ["read_"])
    assert p._apply_tool_search([]) == []
    assert p._apply_tool_search(None) is None


# ── Realistic qanot setup ─────────────────────────────────────


def test_qanot_default_eager_prefixes_match_core_tools():
    """The default eager_tool_prefixes (from Config) should leave all
    qanot core tools eager and defer all domain plugins."""
    from qanot.config import Config
    cfg = Config()
    p = _provider(True, cfg.eager_tool_prefixes)

    # Core tools — should all be eager
    eager_expected = [
        "read_file", "write_file", "list_dir", "run_command",
        "web_search", "web_fetch",
        "memory_search", "recall_lessons",
        "evolve_soul", "verify_lesson", "revoke_lesson",
        "execute_code",
        "send_file", "send_message",
        "session_status",
        "compaction_stats", "cache_stats",
        "spawn_agent", "list_agents",
    ]
    for name in eager_expected:
        assert p._is_eager_tool(name), f"{name!r} should be eager but isn't"

    # Domain tools — should all be deferred
    deferred_expected = [
        "topkey_list_tasks", "topkey_get_task_history",
        "absmarket_query", "absmarket_get_cashier_daily_report",
        "documents_create_xlsx",
    ]
    for name in deferred_expected:
        assert not p._is_eager_tool(name), f"{name!r} should be deferred but is eager"


def test_qanot_default_split_at_50_tools():
    """At 50 tools (our target scale) with default prefixes, deferred
    list should dominate — proving the cost win is real."""
    from qanot.config import Config
    cfg = Config()
    p = _provider(True, cfg.eager_tool_prefixes)
    # Synthesize 50 tools matching real qanot patterns
    tools = []
    for n in ("read_file", "write_file", "list_dir", "run_command",
              "web_search", "web_fetch", "memory_search", "recall_lessons",
              "evolve_soul", "execute_code"):
        tools.append({"name": n, "input_schema": {}})
    for i in range(20):
        tools.append({"name": f"topkey_action_{i}", "input_schema": {}})
    for i in range(20):
        tools.append({"name": f"absmarket_action_{i}", "input_schema": {}})

    out = p._apply_tool_search(tools)
    eager = [t for t in out if "name" in t and "defer_loading" not in t and t.get("type") != "tool_search_tool_bm25_20251119"]
    deferred = [t for t in out if t.get("defer_loading") is True]
    assert len(eager) == 10  # the core set
    assert len(deferred) == 40  # both plugin sets
