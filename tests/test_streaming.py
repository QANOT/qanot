"""Tests for streaming agent loop and provider streaming."""

from __future__ import annotations

import json
import pytest

from qanot.agent import Agent
from qanot.registry import ToolRegistry
from qanot.config import Config
from qanot.providers.base import (
    LLMProvider, ProviderResponse, StreamEvent, ToolCall, Usage,
)


class StreamingFakeProvider(LLMProvider):
    """Provider that yields pre-configured stream events."""

    def __init__(self):
        self.model = "fake-stream"
        self._rounds: list[list[StreamEvent]] = []
        self._round_idx = 0

    def add_round(self, events: list[StreamEvent]) -> None:
        self._rounds.append(events)

    async def chat(self, messages, tools=None, system=None):
        # Collect from stream for non-streaming fallback
        full_text = ""
        tcs = []
        resp = None
        async for ev in self.chat_stream(messages, tools, system):
            if ev.type == "text_delta":
                full_text += ev.text
            elif ev.type == "tool_use" and ev.tool_call:
                tcs.append(ev.tool_call)
            elif ev.type == "done":
                resp = ev.response
        return resp or ProviderResponse(content=full_text, tool_calls=tcs)

    async def chat_stream(self, messages, tools=None, system=None):
        if self._round_idx < len(self._rounds):
            events = self._rounds[self._round_idx]
        else:
            events = [
                StreamEvent(type="text_delta", text="(exhausted)"),
                StreamEvent(type="done", response=ProviderResponse(
                    content="(exhausted)", usage=Usage(1, 1),
                )),
            ]
        self._round_idx += 1
        for ev in events:
            yield ev


def make_config(tmp_path) -> Config:
    return Config(
        workspace_dir=str(tmp_path / "workspace"),
        sessions_dir=str(tmp_path / "sessions"),
        cron_dir=str(tmp_path / "cron"),
    )


class TestRunTurnStream:
    @pytest.mark.asyncio
    async def test_simple_stream(self, tmp_path):
        provider = StreamingFakeProvider()
        provider.add_round([
            StreamEvent(type="text_delta", text="Hello "),
            StreamEvent(type="text_delta", text="world!"),
            StreamEvent(type="done", response=ProviderResponse(
                content="Hello world!", stop_reason="end_turn", usage=Usage(10, 5),
            )),
        ])

        agent = Agent(
            config=make_config(tmp_path),
            provider=provider,
            tool_registry=ToolRegistry(),
        )

        collected = []
        async for event in agent.run_turn_stream("Hi"):
            collected.append(event)

        text_deltas = [e.text for e in collected if e.type == "text_delta"]
        assert text_deltas == ["Hello ", "world!"]

        done_events = [e for e in collected if e.type == "done"]
        assert len(done_events) == 1
        assert done_events[0].response.content == "Hello world!"

    @pytest.mark.asyncio
    async def test_stream_with_tool_use(self, tmp_path):
        provider = StreamingFakeProvider()
        # Round 1: tool call
        provider.add_round([
            StreamEvent(type="text_delta", text="Let me check..."),
            StreamEvent(type="tool_use", tool_call=ToolCall(id="t1", name="ping", input={})),
            StreamEvent(type="done", response=ProviderResponse(
                content="Let me check...", stop_reason="tool_use",
                tool_calls=[ToolCall(id="t1", name="ping", input={})],
                usage=Usage(10, 5),
            )),
        ])
        # Round 2: final answer
        provider.add_round([
            StreamEvent(type="text_delta", text="Done!"),
            StreamEvent(type="done", response=ProviderResponse(
                content="Done!", stop_reason="end_turn", usage=Usage(15, 8),
            )),
        ])

        reg = ToolRegistry()

        async def ping(_):
            return json.dumps({"status": "pong"})

        reg.register("ping", "Ping", {"type": "object"}, ping)

        agent = Agent(
            config=make_config(tmp_path),
            provider=provider,
            tool_registry=reg,
        )

        collected = []
        async for event in agent.run_turn_stream("Do ping"):
            collected.append(event)

        types = [e.type for e in collected]
        assert "text_delta" in types
        assert "tool_use" in types
        assert "done" in types

    @pytest.mark.asyncio
    async def test_stream_per_user_isolation(self, tmp_path):
        provider = StreamingFakeProvider()
        for i in range(3):
            provider.add_round([
                StreamEvent(type="text_delta", text=f"Reply {i}"),
                StreamEvent(type="done", response=ProviderResponse(
                    content=f"Reply {i}", stop_reason="end_turn", usage=Usage(1, 1),
                )),
            ])

        agent = Agent(
            config=make_config(tmp_path),
            provider=provider,
            tool_registry=ToolRegistry(),
        )

        async for _ in agent.run_turn_stream("A1", user_id="a"):
            pass
        async for _ in agent.run_turn_stream("A2", user_id="a"):
            pass
        async for _ in agent.run_turn_stream("B1", user_id="b"):
            pass

        assert len(agent._get_messages("a")) == 4  # 2 user + 2 assistant
        assert len(agent._get_messages("b")) == 2  # 1 user + 1 assistant


class TestStreamEventFallback:
    @pytest.mark.asyncio
    async def test_base_provider_fallback(self):
        """Base LLMProvider.chat_stream() should yield from chat() result."""

        class MinimalProvider(LLMProvider):
            model = "minimal"

            async def chat(self, messages, tools=None, system=None):
                return ProviderResponse(
                    content="fallback text",
                    tool_calls=[ToolCall(id="t1", name="x", input={})],
                    usage=Usage(5, 5),
                )

        provider = MinimalProvider()
        events = []
        async for ev in provider.chat_stream([], None, None):
            events.append(ev)

        types = [e.type for e in events]
        assert types == ["text_delta", "tool_use", "done"]
        assert events[0].text == "fallback text"
        assert events[1].tool_call.name == "x"
        assert events[2].response.content == "fallback text"
