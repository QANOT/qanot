"""Tests for the lifecycle hooks that were defined-but-not-fired before
this PR. Validates Item 3 from the Hermes-borrow list.

We don't spin up the full Agent — the goal is to assert each hook
point fires the right kwargs from the right call site. The pattern
is: build a minimal harness around the function under test, register
a hook handler that captures invocations, exercise the function, assert.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from qanot.hooks import HookRegistry


def test_hook_registry_supports_documented_hook_points():
    """All 7 hook points from the docstring should accept registrations."""
    h = HookRegistry()
    invocations: list[str] = []

    async def handler(**kwargs):
        invocations.append(kwargs.get("hook_point", "?"))

    for hp in (
        "on_startup", "on_shutdown",
        "on_pre_turn", "on_post_turn",
        "on_tool_use", "on_error", "on_compaction",
    ):
        h.register(hp, handler, name="test_plugin")

    asyncio.run(h.fire("on_tool_use", tool_name="x", result="y"))
    asyncio.run(h.fire("on_error", error_type="rate_limit", error="x"))
    asyncio.run(h.fire("on_compaction", tokens_before=100, summary_chars=20))
    # No exception = ok; the kwargs are passed through.


def test_on_tool_use_captures_kwargs():
    h = HookRegistry()
    seen: list[dict] = []

    async def capture(**kwargs):
        seen.append(kwargs)

    h.register("on_tool_use", capture, name="test")
    asyncio.run(h.fire(
        "on_tool_use",
        tool_name="topkey_list_tasks",
        tool_input={"assigned_to": 854},
        result='{"items": []}',
        duration_ms=120,
        error=None,
        user_id="user42",
    ))
    assert len(seen) == 1
    assert seen[0]["tool_name"] == "topkey_list_tasks"
    assert seen[0]["duration_ms"] == 120
    assert seen[0]["error"] is None


def test_on_compaction_fires_with_metrics():
    h = HookRegistry()
    seen: list[dict] = []

    async def capture(**kwargs):
        seen.append(kwargs)

    h.register("on_compaction", capture, name="test")
    asyncio.run(h.fire(
        "on_compaction",
        tokens_before=15000,
        summary_chars=2500,
        duration_ms=4200,
        stage="full",
        error="",
    ))
    assert seen[0]["stage"] == "full"
    assert seen[0]["tokens_before"] == 15000


def test_on_error_kwargs_include_recoverable_flag():
    h = HookRegistry()
    seen: list[dict] = []

    async def capture(**kwargs):
        seen.append(kwargs)

    h.register("on_error", capture, name="test")
    asyncio.run(h.fire(
        "on_error",
        error_type="context_overflow",
        error="too big",
        user_id="user42",
        recoverable=True,
    ))
    assert seen[0]["recoverable"] is True


def test_hook_failure_in_one_handler_does_not_affect_others():
    """Defensive guarantee: a buggy plugin's hook handler must not
    break the agent loop or starve other plugins' handlers."""
    h = HookRegistry()
    success_count = [0]

    async def buggy(**kwargs):
        raise RuntimeError("plugin is angry")

    async def good(**kwargs):
        success_count[0] += 1

    h.register("on_tool_use", buggy, name="buggy_plugin")
    h.register("on_tool_use", good, name="good_plugin")

    # Should not raise even though buggy raises.
    asyncio.run(h.fire("on_tool_use", tool_name="x"))
    assert success_count[0] == 1


def test_summarize_for_compaction_fires_on_compaction_hook(tmp_path):
    """Direct integration test: call summarize_for_compaction with a
    HookRegistry and assert on_compaction fires with the expected
    metrics envelope.
    """
    from qanot.flush import summarize_for_compaction
    from qanot.hooks import HookRegistry

    class _FakeProvider:
        async def chat(self, messages, tools=None, system=None):
            class _R:
                content = "summarized\nbody\nhere"
            return _R()

    class _FakeConfig:
        compaction_mode = "multi_stage"
        max_context_tokens = 200_000
        workspace_dir = str(tmp_path)

    class _FakeContext:
        def extract_compaction_text(self, messages):
            return "extracted"

    fake_messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ] + [
        {"role": "user", "content": f"msg {i}"}
        for i in range(20)
    ] + [
        {"role": "assistant", "content": "ok"},
    ]

    h = HookRegistry()
    captured: list[dict] = []

    async def capture(**kwargs):
        captured.append(kwargs)

    h.register("on_compaction", capture, name="test_handler")

    asyncio.run(summarize_for_compaction(
        fake_messages, _FakeProvider(), _FakeConfig(), _FakeContext(),
        hooks=h,
    ))

    # Hook should have fired exactly once with metrics.
    assert len(captured) == 1
    event = captured[0]
    assert "tokens_before" in event
    assert "summary_chars" in event
    assert "duration_ms" in event
    assert "stage" in event
