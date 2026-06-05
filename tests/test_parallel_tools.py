"""A2: read-safe tool batches execute concurrently, preserving order."""

from __future__ import annotations

import asyncio
import time

import pytest

from qanot.agent.loop import _LoopMixin, _PARALLEL_SAFE_TOOLS
from qanot.providers.base import ToolCall


class _Host(_LoopMixin):
    def __init__(self, per_tool_delay=0.05):
        self._delay = per_tool_delay
        self.executed_order: list[str] = []

        class _Tools:
            async def execute(_self, name, inp, *, timeout, workspace_dir):
                await asyncio.sleep(self._delay)
                self.executed_order.append(name)
                return f'{{"tool":"{name}"}}'

        class _Hooks:
            async def fire(self, *a, **k):
                return None

        self.tools = _Tools()
        self.hooks = _Hooks()
        self.config = type("C", (), {"workspace_dir": "/tmp"})()
        self.current_user_id = "u1"


def _calls(*names):
    return [ToolCall(id=f"t{i}", name=n, input={}) for i, n in enumerate(names)]


@pytest.mark.asyncio
async def test_read_safe_batch_runs_concurrently_and_in_order():
    # 3 web_search calls (all read-safe) — should run in parallel.
    host = _Host(per_tool_delay=0.05)
    calls = _calls("web_search", "web_search", "read_file")
    assert all(n in _PARALLEL_SAFE_TOOLS for n in ("web_search", "read_file"))

    t0 = time.perf_counter()
    blocks, _hash = await host._execute_tools(calls)
    elapsed = time.perf_counter() - t0

    # 3 × 0.05s sequential = 0.15s; concurrent ≈ 0.05s.
    assert elapsed < 0.12, f"expected concurrent (<0.12s), took {elapsed:.3f}s"
    # tool_result blocks line up with the input tool_use ids, in order.
    assert [b["tool_use_id"] for b in blocks] == ["t0", "t1", "t2"]


@pytest.mark.asyncio
async def test_batch_with_unsafe_tool_runs_sequentially():
    host = _Host(per_tool_delay=0.05)
    # send_file is NOT read-safe → whole batch serializes.
    calls = _calls("web_search", "send_file")

    t0 = time.perf_counter()
    blocks, _hash = await host._execute_tools(calls)
    elapsed = time.perf_counter() - t0

    assert elapsed >= 0.09, f"expected sequential (>=0.09s), took {elapsed:.3f}s"
    assert host.executed_order == ["web_search", "send_file"]  # strict order
    assert [b["tool_use_id"] for b in blocks] == ["t0", "t1"]


@pytest.mark.asyncio
async def test_single_tool_not_parallelized():
    host = _Host(per_tool_delay=0.01)
    blocks, _hash = await host._execute_tools(_calls("web_search"))
    assert len(blocks) == 1 and blocks[0]["tool_use_id"] == "t0"


@pytest.mark.asyncio
async def test_result_blocks_carry_content():
    host = _Host(per_tool_delay=0.0)
    blocks, combined = await host._execute_tools(_calls("read_file", "memory_search"))
    assert blocks[0]["content"] == '{"tool":"read_file"}'
    assert blocks[1]["content"] == '{"tool":"memory_search"}'
    assert combined  # fingerprint computed
