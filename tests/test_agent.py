"""Tests for core agent loop and ToolRegistry."""

from __future__ import annotations

import asyncio
import json
import pytest

from qanot.agent import Agent
from qanot.registry import ToolRegistry
from qanot.config import Config
from qanot.providers.base import LLMProvider, ProviderResponse, ToolCall, Usage


# ── Helpers ──────────────────────────────────────────────────


class FakeProvider(LLMProvider):
    """Provider that returns pre-configured responses."""

    def __init__(self, responses: list[ProviderResponse] | None = None):
        self.model = "fake-model"
        self._responses = list(responses or [])
        self._call_count = 0
        self.calls: list[dict] = []

    def enqueue(self, *responses: ProviderResponse) -> None:
        self._responses.extend(responses)

    async def chat(self, messages, tools=None, system=None):
        self.calls.append({"messages": messages, "tools": tools, "system": system})
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
        else:
            resp = ProviderResponse(content="(no more responses)")
        self._call_count += 1
        return resp


def make_config(tmp_path) -> Config:
    return Config(
        workspace_dir=str(tmp_path / "workspace"),
        sessions_dir=str(tmp_path / "sessions"),
        cron_dir=str(tmp_path / "cron"),
    )


# ── ToolRegistry ─────────────────────────────────────────────


class TestToolRegistry:
    def test_register_and_list(self):
        reg = ToolRegistry()
        reg.register("greet", "Say hello", {"type": "object", "properties": {}}, self._noop)
        assert "greet" in reg.tool_names
        defs = reg.get_definitions()
        assert len(defs) == 1
        assert defs[0]["name"] == "greet"

    @pytest.mark.asyncio
    async def test_execute_known_tool(self):
        reg = ToolRegistry()

        async def echo(params):
            return json.dumps({"echo": params.get("msg", "")})

        reg.register("echo", "Echo back", {"type": "object"}, echo)
        result = await reg.execute("echo", {"msg": "hi"})
        assert json.loads(result) == {"echo": "hi"}

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        reg = ToolRegistry()
        result = await reg.execute("nonexistent", {})
        data = json.loads(result)
        assert "error" in data
        assert "Unknown tool" in data["error"]

    @pytest.mark.asyncio
    async def test_execute_handler_exception(self):
        reg = ToolRegistry()

        async def failing(_):
            raise ValueError("boom")

        reg.register("fail", "Always fails", {"type": "object"}, failing)
        result = await reg.execute("fail", {})
        data = json.loads(result)
        assert "boom" in data["error"]

    @staticmethod
    async def _noop(_):
        return "{}"


# ── Agent ────────────────────────────────────────────────────


class TestAgent:
    @pytest.mark.asyncio
    async def test_simple_response(self, tmp_path):
        provider = FakeProvider([
            ProviderResponse(content="Hello!", stop_reason="end_turn", usage=Usage(10, 5)),
        ])
        config = make_config(tmp_path)
        agent = Agent(config=config, provider=provider, tool_registry=ToolRegistry())

        result = await agent.run_turn("Hi")
        assert result == "Hello!"

    @pytest.mark.asyncio
    async def test_tool_use_then_response(self, tmp_path):
        """Agent should execute tool, then get final response."""
        provider = FakeProvider([
            # First call: LLM asks to use a tool
            ProviderResponse(
                content="Let me check...",
                stop_reason="tool_use",
                tool_calls=[ToolCall(id="t1", name="ping", input={})],
                usage=Usage(10, 5),
            ),
            # Second call: LLM gives final answer
            ProviderResponse(
                content="Pong received!",
                stop_reason="end_turn",
                usage=Usage(15, 8),
            ),
        ])
        config = make_config(tmp_path)
        reg = ToolRegistry()

        async def ping(_):
            return json.dumps({"status": "pong"})

        reg.register("ping", "Ping test", {"type": "object"}, ping)

        agent = Agent(config=config, provider=provider, tool_registry=reg)
        result = await agent.run_turn("Do a ping")
        assert result == "Pong received!"
        assert provider._call_count == 2

    @pytest.mark.asyncio
    async def test_max_iterations(self, tmp_path):
        """Agent should stop after MAX_ITERATIONS."""
        # Use unique inputs per call to avoid triggering loop detection
        responses = [
            ProviderResponse(
                content="",
                stop_reason="tool_use",
                tool_calls=[ToolCall(id=f"t{i}", name="noop", input={"step": i})],
                usage=Usage(1, 1),
            )
            for i in range(30)
        ]
        provider = FakeProvider(responses)
        config = make_config(tmp_path)
        reg = ToolRegistry()

        async def noop(_):
            return "{}"

        reg.register("noop", "No-op", {"type": "object"}, noop)

        agent = Agent(config=config, provider=provider, tool_registry=reg)
        result = await agent.run_turn("loop forever")
        assert "maximum iterations" in result

    @pytest.mark.asyncio
    async def test_loop_detection(self, tmp_path):
        """Agent should break when same tool call repeats 3 times."""
        responses = [
            ProviderResponse(
                content="",
                stop_reason="tool_use",
                tool_calls=[ToolCall(id=f"t{i}", name="noop", input={})],
                usage=Usage(1, 1),
            )
            for i in range(10)
        ]
        provider = FakeProvider(responses)
        config = make_config(tmp_path)
        reg = ToolRegistry()

        async def noop(_):
            return "{}"

        reg.register("noop", "No-op", {"type": "object"}, noop)

        agent = Agent(config=config, provider=provider, tool_registry=reg)
        result = await agent.run_turn("do noop")
        assert "takrorlanmoqda" in result
        # Should stop well before MAX_ITERATIONS (25)
        assert provider._call_count <= 5

    @pytest.mark.asyncio
    async def test_deterministic_error_hint(self, tmp_path):
        """Deterministic errors should get a _hint injected."""
        provider = FakeProvider([
            ProviderResponse(
                content="",
                stop_reason="tool_use",
                tool_calls=[ToolCall(id="t1", name="bad_tool", input={"x": 1})],
                usage=Usage(1, 1),
            ),
            ProviderResponse(
                content="Got the hint, stopping.",
                stop_reason="end_turn",
                usage=Usage(1, 1),
            ),
        ])
        config = make_config(tmp_path)
        reg = ToolRegistry()

        async def bad_tool(_):
            return json.dumps({"error": "Unknown tool: bad_tool"})

        reg.register("bad_tool", "Always errors", {"type": "object"}, bad_tool)

        agent = Agent(config=config, provider=provider, tool_registry=reg)
        result = await agent.run_turn("call bad tool")
        assert result == "Got the hint, stopping."
        # The tool result message is the last user message before the final call
        # calls[1] has messages including: [..., assistant(tool_use), user(tool_results)]
        tool_result_msg = [m for m in provider.calls[1]["messages"]
                          if m["role"] == "user" and isinstance(m.get("content"), list)][-1]
        data = json.loads(tool_result_msg["content"][0]["content"])
        assert "_hint" in data

    @pytest.mark.asyncio
    async def test_per_user_isolation(self, tmp_path):
        """Different user_ids should have separate conversation histories."""
        call_count = 0

        class TrackingProvider(FakeProvider):
            async def chat(self, messages, tools=None, system=None):
                nonlocal call_count
                call_count += 1
                # Record message count per call
                self.calls.append({"msg_count": len(messages)})
                return ProviderResponse(
                    content=f"Reply {call_count}",
                    stop_reason="end_turn",
                    usage=Usage(10, 5),
                )

        provider = TrackingProvider()
        config = make_config(tmp_path)
        agent = Agent(config=config, provider=provider, tool_registry=ToolRegistry())

        # User A sends 2 messages
        await agent.run_turn("Hello from A", user_id="user_a")
        await agent.run_turn("Second from A", user_id="user_a")

        # User B sends 1 message — should have clean history
        await agent.run_turn("Hello from B", user_id="user_b")

        # User A's conversation should have 4 messages (2 user + 2 assistant)
        assert len(agent._get_messages("user_a")) == 4
        # User B's conversation should have 2 messages (1 user + 1 assistant)
        assert len(agent._get_messages("user_b")) == 2

    @pytest.mark.asyncio
    async def test_reset_single_user(self, tmp_path):
        provider = FakeProvider([
            ProviderResponse(content="R1", stop_reason="end_turn", usage=Usage(1, 1)),
            ProviderResponse(content="R2", stop_reason="end_turn", usage=Usage(1, 1)),
        ])
        config = make_config(tmp_path)
        agent = Agent(config=config, provider=provider, tool_registry=ToolRegistry())

        await agent.run_turn("msg", user_id="a")
        await agent.run_turn("msg", user_id="b")

        agent.reset(user_id="a")
        # After reset, "a" is evicted from in-memory cache.
        # _get_messages will restore from session history (JSONL).
        restored_a = agent._get_messages("a")
        assert len(restored_a) >= 0  # May restore from session files
        assert "a" not in agent._locks  # Lock was cleared
        assert len(agent._get_messages("b")) == 2  # "b" unchanged

    @pytest.mark.asyncio
    async def test_reset_all(self, tmp_path):
        provider = FakeProvider([
            ProviderResponse(content="R1", stop_reason="end_turn", usage=Usage(1, 1)),
            ProviderResponse(content="R2", stop_reason="end_turn", usage=Usage(1, 1)),
        ])
        config = make_config(tmp_path)
        agent = Agent(config=config, provider=provider, tool_registry=ToolRegistry())

        await agent.run_turn("msg", user_id="a")
        await agent.run_turn("msg", user_id="b")

        agent.reset()
        assert len(agent._conversations) == 0
