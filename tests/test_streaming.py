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


class TestRespondStreamFinalText:
    """Final-text fallback in _respond_stream — must NOT send the generic
    'Xatolik yuz berdi' error when the model legitimately ends with no text
    after a burst of tool calls (e.g. 5×tg_send_poll → end_turn empty).
    Regression: 2026-05-28 thread 222670 incident.
    """

    @pytest.fixture
    def mixin(self):
        from qanot.telegram.streaming import StreamingMixin

        class _Fake(StreamingMixin):
            def __init__(self):
                self._draft_counter = 0
                self._draft_sends = []
                self._final_sends = []
                self.config = type("C", (), {"stream_flush_interval": 9999.0})()
                self.bot = None
                self.agent = None

            async def _typing_loop(self, chat_id):
                # cooperate with cancel()
                try:
                    while True:
                        await __import__("asyncio").sleep(3600)
                except __import__("asyncio").CancelledError:
                    return

            async def _send_draft(self, chat_id, draft_id, text, *, thread_id=None):
                self._draft_sends.append(text)

            async def _send_final(self, chat_id, text, *, reply_to=None, thread_id=None):
                self._final_sends.append(text)

        return _Fake()

    def _stub_agent(self, events):
        async def run_turn_stream(*args, **kwargs):
            for ev in events:
                yield ev

        return type("A", (), {"run_turn_stream": staticmethod(run_turn_stream)})()

    @pytest.mark.asyncio
    async def test_no_error_when_tools_ended_turn_silently(self, mixin):
        """Model emits tool_use events then ends with empty content —
        send nothing (tools spoke for the model). Must NOT send 'Xatolik'."""
        mixin.agent = self._stub_agent([
            StreamEvent(type="tool_use", tool_call=ToolCall(id="t1", name="tg_send_poll", input={})),
            StreamEvent(type="tool_use", tool_call=ToolCall(id="t2", name="tg_send_poll", input={})),
            StreamEvent(type="done", response=ProviderResponse(
                content="", stop_reason="end_turn", usage=Usage(10, 0),
            )),
        ])
        await mixin._respond_stream(1, "u", "hi")
        assert mixin._final_sends == []  # nothing sent — silent end is correct
        assert all("Xatolik" not in s for s in mixin._draft_sends)

    @pytest.mark.asyncio
    async def test_error_when_no_tools_and_no_text(self, mixin):
        """Genuine empty response (no tool_use, no text) — fallback fires."""
        mixin.agent = self._stub_agent([
            StreamEvent(type="done", response=ProviderResponse(
                content="", stop_reason="end_turn", usage=Usage(1, 0),
            )),
        ])
        await mixin._respond_stream(1, "u", "hi")
        assert len(mixin._final_sends) == 1
        assert "Xatolik yuz berdi" in mixin._final_sends[0]

    @pytest.mark.asyncio
    async def test_uses_accumulated_text(self, mixin):
        """Normal path: streamed text becomes the final message."""
        mixin.agent = self._stub_agent([
            StreamEvent(type="text_delta", text="Hello "),
            StreamEvent(type="text_delta", text="world"),
            StreamEvent(type="done", response=ProviderResponse(
                content="Hello world", stop_reason="end_turn", usage=Usage(5, 2),
            )),
        ])
        await mixin._respond_stream(1, "u", "hi")
        assert mixin._final_sends == ["Hello world"]


class TestToolProgressBubbles:
    """Per-tool progress bubbles + auto-cleanup (#1, #4)."""

    def _mixin(self, mode: str = "minimal", cleanup: bool = True, heartbeat: int = 0):
        from qanot.telegram.streaming import StreamingMixin

        class _Fake(StreamingMixin):
            def __init__(self):
                self._draft_counter = 0
                self._final_sends = []
                self.progress_sends = []      # (text)
                self.deleted = []             # message_ids deleted
                self._next_mid = 100
                self.config = type("C", (), {
                    "stream_flush_interval": 9999.0,
                    "tool_progress": mode,
                    "tool_progress_cleanup": cleanup,
                    "long_run_notice_seconds": heartbeat,
                })()
                self.bot = None
                self.agent = None

            async def _typing_loop(self, chat_id):
                try:
                    while True:
                        await __import__("asyncio").sleep(3600)
                except __import__("asyncio").CancelledError:
                    return

            async def _send_draft(self, chat_id, draft_id, text, *, thread_id=None):
                pass

            async def _send_final(self, chat_id, text, *, reply_to=None, thread_id=None):
                self._final_sends.append(text)

            async def _send_tool_progress(self, chat_id, text, *, thread_id=None):
                self.progress_sends.append(text)
                self._next_mid += 1
                return self._next_mid

            async def _delete_messages(self, chat_id, message_ids):
                self.deleted.extend(message_ids)

        return _Fake()

    def _stub_agent(self, events):
        async def run_turn_stream(*args, **kwargs):
            for ev in events:
                yield ev
        return type("A", (), {"run_turn_stream": staticmethod(run_turn_stream)})()

    @pytest.mark.asyncio
    async def test_minimal_mode_one_bubble_per_distinct_tool_then_cleanup(self):
        mixin = self._mixin("minimal")
        mixin.agent = self._stub_agent([
            StreamEvent(type="tool_use", tool_call=ToolCall(id="t1", name="web_search", input={"query": "x"})),
            StreamEvent(type="tool_use", tool_call=ToolCall(id="t2", name="web_search", input={"query": "y"})),  # dedup
            StreamEvent(type="tool_use", tool_call=ToolCall(id="t3", name="read_file", input={"path": "/a"})),
            StreamEvent(type="text_delta", text="done"),
            StreamEvent(type="done", response=ProviderResponse(content="done", stop_reason="end_turn", usage=Usage(1, 1))),
        ])
        await mixin._respond_stream(1, "u", "hi")
        # minimal: no args, consecutive web_search deduped → 2 bubbles
        assert mixin.progress_sends == ["🔍 web_search…", "📖 read_file…"]
        assert mixin._final_sends == ["done"]
        assert len(mixin.deleted) == 2  # both cleaned up on success

    @pytest.mark.asyncio
    async def test_detailed_mode_includes_arg_preview(self):
        mixin = self._mixin("detailed")
        mixin.agent = self._stub_agent([
            StreamEvent(type="tool_use", tool_call=ToolCall(id="t1", name="web_search", input={"query": "weather Tashkent"})),
            StreamEvent(type="done", response=ProviderResponse(content="ok", stop_reason="end_turn", usage=Usage(1, 1))),
        ])
        await mixin._respond_stream(1, "u", "hi")
        assert mixin.progress_sends == ["🔍 web_search: weather Tashkent"]

    @pytest.mark.asyncio
    async def test_off_mode_sends_no_bubbles(self):
        mixin = self._mixin("off")
        mixin.agent = self._stub_agent([
            StreamEvent(type="tool_use", tool_call=ToolCall(id="t1", name="web_search", input={"query": "x"})),
            StreamEvent(type="done", response=ProviderResponse(content="ok", stop_reason="end_turn", usage=Usage(1, 1))),
        ])
        await mixin._respond_stream(1, "u", "hi")
        assert mixin.progress_sends == []
        assert mixin.deleted == []

    @pytest.mark.asyncio
    async def test_error_path_keeps_breadcrumbs(self):
        """No text + no done content + tools didn't speak → error reply, bubbles kept."""
        mixin = self._mixin("minimal")
        mixin.agent = self._stub_agent([
            StreamEvent(type="done", response=ProviderResponse(content="", stop_reason="end_turn", usage=Usage(1, 0))),
        ])
        # No tool_use here → error fallback; nothing to clean. Now with a tool but
        # an empty error-shaped end is covered by the silent-tools test elsewhere.
        await mixin._respond_stream(1, "u", "hi")
        assert "Xatolik yuz berdi" in mixin._final_sends[0]
        assert mixin.deleted == []

    @pytest.mark.asyncio
    async def test_heartbeat_fires_during_long_turn_and_cleans_up(self):
        """With a short notice interval and a slow turn, the '⏳' heartbeat
        fires at least once and its messages are cleaned up on success (#2)."""
        import asyncio

        mixin = self._mixin("off", heartbeat=1)  # 1s interval (clamped int floor)
        # patch sleep so the 1s heartbeat interval elapses fast in the test
        real_sleep = asyncio.sleep

        async def fast_sleep(secs):
            await real_sleep(min(secs, 0.02))

        async def slow_stream(*args, **kwargs):
            await real_sleep(0.07)  # ~3 heartbeat ticks at 0.02 each
            yield StreamEvent(type="text_delta", text="ok")
            yield StreamEvent(type="done", response=ProviderResponse(
                content="ok", stop_reason="end_turn", usage=Usage(1, 1)))

        mixin.agent = type("A", (), {"run_turn_stream": staticmethod(slow_stream)})()
        import qanot.telegram.streaming as _s
        orig = _s.asyncio.sleep
        _s.asyncio.sleep = fast_sleep
        try:
            await mixin._respond_stream(1, "u", "hi")
        finally:
            _s.asyncio.sleep = orig
        assert any("Ishlayapman" in t for t in mixin.progress_sends)
        assert len(mixin.deleted) >= 1  # heartbeat bubbles cleaned up
        assert mixin._final_sends == ["ok"]


class TestChunkIndicators:
    """Split long replies get (i/n) footers (#7)."""

    def _mixin(self):
        from qanot.telegram.streaming import StreamingMixin

        class _RecBot:
            def __init__(self):
                self.sent = []

            async def send_message(self, **kw):
                self.sent.append(kw.get("text", ""))
                return type("M", (), {"message_id": len(self.sent)})()

        class _Fake(StreamingMixin):
            def __init__(self):
                self.bot = _RecBot()
                self.agent = None
                self.config = type("C", (), {"workspace_dir": "/tmp"})()

        return _Fake()

    @pytest.mark.asyncio
    async def test_single_chunk_has_no_indicator(self):
        mixin = self._mixin()
        await mixin._send_final(1, "short reply")
        assert len(mixin.bot.sent) == 1
        assert "(1/" not in mixin.bot.sent[0]

    @pytest.mark.asyncio
    async def test_multi_chunk_gets_numbered_footers(self):
        mixin = self._mixin()
        # Force a split: a paragraph well over MAX_MSG_LEN with newline cut points.
        from qanot.telegram.formatting import MAX_MSG_LEN
        para = ("lorem ipsum dolor sit amet\n" * ((MAX_MSG_LEN // 26) + 50))
        await mixin._send_final(1, para)
        assert len(mixin.bot.sent) >= 2
        n = len(mixin.bot.sent)
        for i, body in enumerate(mixin.bot.sent, start=1):
            assert f"({i}/{n})" in body


class TestMidTurnSteer:
    """A4: /steer injects a note into the running turn without killing it."""

    def test_add_steer_queues(self, tmp_path):
        agent = Agent(config=make_config(tmp_path), provider=StreamingFakeProvider(),
                      tool_registry=ToolRegistry())
        assert agent.add_steer("u1", "") is False        # empty → not queued
        assert agent.add_steer("u1", "use staging") is True
        assert agent._pending_steer["u1"] == ["use staging"]

    @pytest.mark.asyncio
    async def test_steer_injected_into_tool_result(self, tmp_path):
        provider = StreamingFakeProvider()
        provider.add_round([
            StreamEvent(type="tool_use", tool_call=ToolCall(id="t1", name="ping", input={})),
            StreamEvent(type="done", response=ProviderResponse(
                content="", stop_reason="tool_use",
                tool_calls=[ToolCall(id="t1", name="ping", input={})], usage=Usage(10, 5))),
        ])
        provider.add_round([
            StreamEvent(type="done", response=ProviderResponse(
                content="ok", stop_reason="end_turn", usage=Usage(5, 2))),
        ])
        reg = ToolRegistry()
        holder: dict = {}

        async def ping(_):
            # simulate a /steer arriving while the tool runs
            holder["agent"].add_steer("u1", "use the staging DB")
            return json.dumps({"status": "pong"})

        reg.register("ping", "Ping", {"type": "object"}, ping)
        agent = Agent(config=make_config(tmp_path), provider=provider, tool_registry=reg)
        holder["agent"] = agent

        async for _ in agent.run_turn_stream("do ping", user_id="u1"):
            pass

        # the steer note must appear as a text block in some user message
        msgs = agent._get_messages("u1")
        blob = json.dumps(msgs, default=str)
        assert "use the staging DB" in blob
        assert agent._pending_steer.get("u1") in (None, [])  # drained

    @pytest.mark.asyncio
    async def test_stale_steer_cleared_at_turn_start(self, tmp_path):
        provider = StreamingFakeProvider()
        provider.add_round([
            StreamEvent(type="done", response=ProviderResponse(
                content="hi", stop_reason="end_turn", usage=Usage(3, 1))),
        ])
        agent = Agent(config=make_config(tmp_path), provider=provider,
                      tool_registry=ToolRegistry())
        agent._pending_steer["u1"] = ["stale note from before"]
        async for _ in agent.run_turn_stream("hello", user_id="u1"):
            pass
        # a steer with no in-flight turn must not leak into this fresh turn
        assert agent._pending_steer.get("u1") in (None, [])
