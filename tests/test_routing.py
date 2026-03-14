"""Tests for model routing — complexity classifier and RoutingProvider."""

from __future__ import annotations

import asyncio
import pytest

from qanot.routing import classify_complexity, RoutingProvider, RoutingStats
from qanot.providers.base import LLMProvider, ProviderResponse, StreamEvent, Usage


# --- Classifier tests --------------------------------------------------------

class TestClassifyComplexity:
    """Test the heuristic message complexity classifier."""

    # Simple messages (should score < 0.3)

    def test_empty_message(self):
        assert classify_complexity("") == 0.0

    def test_uzbek_greeting_salom(self):
        assert classify_complexity("salom") < 0.1

    def test_uzbek_greeting_assalomu_alaykum(self):
        assert classify_complexity("assalomu alaykum") < 0.1

    def test_uzbek_acknowledgment_rahmat(self):
        assert classify_complexity("rahmat") < 0.1

    def test_uzbek_yes(self):
        assert classify_complexity("ha") < 0.1

    def test_uzbek_no(self):
        assert classify_complexity("yo'q") < 0.1

    def test_russian_greeting(self):
        assert classify_complexity("привет") < 0.1

    def test_russian_thanks(self):
        assert classify_complexity("спасибо") < 0.1

    def test_english_hello(self):
        assert classify_complexity("hello") < 0.1

    def test_english_thanks(self):
        assert classify_complexity("thank you") < 0.1

    def test_english_ok(self):
        assert classify_complexity("ok") < 0.1

    def test_greeting_with_punctuation(self):
        assert classify_complexity("salom!") < 0.1

    def test_greeting_case_insensitive(self):
        assert classify_complexity("SALOM") < 0.1

    # Medium messages (should score 0.1-0.5)

    def test_short_question(self):
        score = classify_complexity("qanday ahvollar?")
        assert 0.1 <= score <= 0.5

    def test_short_sentence(self):
        score = classify_complexity("bugun ob-havo qanday?")
        assert 0.1 <= score <= 0.5

    # Complex messages (should score >= 0.3)

    def test_long_message(self):
        msg = "Men sizga loyiha haqida batafsil aytib bermoqchiman. " * 10
        score = classify_complexity(msg)
        assert score >= 0.3

    def test_code_block(self):
        msg = "```python\ndef hello():\n    print('hello')\n```"
        score = classify_complexity(msg)
        assert score >= 0.3

    def test_url_in_message(self):
        msg = "Check this URL https://example.com/api/v1/users"
        score = classify_complexity(msg)
        assert score >= 0.3

    def test_technical_request(self):
        msg = "explain how the authentication middleware handles JWT token refresh"
        score = classify_complexity(msg)
        assert score >= 0.3

    def test_multiline_request(self):
        msg = "First do this\nThen do that\nFinally check results"
        score = classify_complexity(msg)
        assert score >= 0.3

    def test_implementation_request(self):
        msg = "implement a function that calculates fibonacci numbers"
        score = classify_complexity(msg)
        assert score >= 0.3

    def test_score_capped_at_1(self):
        msg = "```python\n" + "x = 1\n" * 100 + "```\n" + "https://example.com\n" * 10
        score = classify_complexity(msg)
        assert score <= 1.0

    def test_json_data(self):
        msg = '{"users": [{"id": 12345, "name": "test"}]}'
        score = classify_complexity(msg)
        assert score >= 0.3


# --- Fake provider for testing -----------------------------------------------

class FakeProvider(LLMProvider):
    """Minimal provider that records which model was used."""

    def __init__(self, model: str = "claude-sonnet-4-6"):
        self.model = model
        self.last_model_used: str = ""
        self.call_count: int = 0

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> ProviderResponse:
        self.last_model_used = self.model
        self.call_count += 1
        return ProviderResponse(
            content="test response",
            usage=Usage(input_tokens=100, output_tokens=50),
        )

    async def chat_stream(self, messages, tools=None, system=None):
        self.last_model_used = self.model
        self.call_count += 1
        yield StreamEvent(type="text_delta", text="test")
        yield StreamEvent(
            type="done",
            response=ProviderResponse(
                content="test",
                usage=Usage(input_tokens=100, output_tokens=50),
            ),
        )


# --- RoutingProvider tests ---------------------------------------------------

class TestRoutingProvider:
    """Test the RoutingProvider wrapper."""

    def _make_messages(self, text: str) -> list[dict]:
        return [{"role": "user", "content": text}]

    @pytest.mark.asyncio
    async def test_simple_message_routes_to_cheap(self):
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, cheap_model="claude-haiku-4-5-20251001", threshold=0.3)

        await router.chat(self._make_messages("salom"))

        assert fake.last_model_used == "claude-haiku-4-5-20251001"
        # Model should be restored after call
        assert fake.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_complex_message_routes_to_primary(self):
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, cheap_model="claude-haiku-4-5-20251001", threshold=0.3)

        await router.chat(self._make_messages("explain the authentication flow in detail"))

        assert fake.last_model_used == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_stream_routes_simple(self):
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, cheap_model="claude-haiku-4-5-20251001", threshold=0.3)

        events = []
        async for event in router.chat_stream(self._make_messages("rahmat")):
            events.append(event)

        assert fake.last_model_used == "claude-haiku-4-5-20251001"
        assert any(e.type == "text_delta" for e in events)
        # Model restored
        assert fake.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_stream_routes_complex(self):
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, cheap_model="claude-haiku-4-5-20251001", threshold=0.3)

        events = []
        async for event in router.chat_stream(self._make_messages("implement a REST API endpoint")):
            events.append(event)

        assert fake.last_model_used == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_stats_tracking(self):
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, threshold=0.3)

        await router.chat(self._make_messages("salom"))
        await router.chat(self._make_messages("hello"))
        await router.chat(self._make_messages("implement complex feature"))

        assert router.stats.total == 3
        assert router.stats.routed_cheap == 2
        assert router.stats.routed_primary == 1
        assert router.stats.savings_pct == pytest.approx(66.7, abs=0.1)

    @pytest.mark.asyncio
    async def test_model_restored_on_error(self):
        class ErrorProvider(FakeProvider):
            async def chat(self, messages, tools=None, system=None):
                self.last_model_used = self.model
                raise RuntimeError("API error")

        fake = ErrorProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, threshold=0.3)

        with pytest.raises(RuntimeError):
            await router.chat(self._make_messages("salom"))

        # Model should still be restored after error
        assert fake.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_content_blocks_extraction(self):
        """Test that content blocks (list format) are correctly parsed."""
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, threshold=0.3)

        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": "salom"}],
        }]
        await router.chat(messages)

        assert fake.last_model_used == "claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_no_user_message_uses_primary(self):
        """When no user message found, default to primary model."""
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, threshold=0.3)

        messages = [{"role": "assistant", "content": "hello"}]
        await router.chat(messages)

        # Empty text → score 0.0 → routes to cheap
        assert fake.last_model_used == "claude-haiku-4-5-20251001"

    def test_status_report(self):
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, cheap_model="claude-haiku-4-5-20251001", threshold=0.3)

        status = router.status()
        assert status["primary_model"] == "claude-sonnet-4-6"
        assert status["cheap_model"] == "claude-haiku-4-5-20251001"
        assert status["threshold"] == 0.3
        assert status["stats"]["total"] == 0

    @pytest.mark.asyncio
    async def test_custom_threshold(self):
        """Higher threshold means more messages go to primary."""
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, threshold=0.05)  # Very low threshold

        # Short question — would normally be simple, but threshold is very low
        await router.chat(self._make_messages("qanday?"))

        # "qanday?" scores ~0.15, which is above 0.05 threshold → primary
        assert fake.last_model_used == "claude-sonnet-4-6"


class TestContextAwareRouting:
    """Test that conversation context prevents misrouting simple replies."""

    @pytest.mark.asyncio
    async def test_ha_after_tool_use_stays_primary(self):
        """'ha' reply after agent used tools → must stay on primary model."""
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, threshold=0.3)

        messages = [
            {"role": "user", "content": "implement database migration"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "I'll run the migration now."},
                {"type": "tool_use", "id": "t1", "name": "run_command", "input": {"cmd": "migrate"}},
            ]},
            {"role": "tool", "content": "Migration completed", "tool_use_id": "t1"},
            {"role": "assistant", "content": "Migration done. Should I proceed?"},
            {"role": "user", "content": "ha"},
        ]
        await router.chat(messages)
        assert fake.last_model_used == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_salom_in_fresh_conversation_uses_cheap(self):
        """'salom' as first message → cheap model is fine."""
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, threshold=0.3)

        messages = [{"role": "user", "content": "salom"}]
        await router.chat(messages)
        assert fake.last_model_used == "claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_ok_after_long_assistant_response_stays_primary(self):
        """'ok' after a long assistant response → complex context."""
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, threshold=0.3)

        messages = [
            {"role": "user", "content": "explain authentication"},
            {"role": "assistant", "content": "x" * 600},  # Long explanation
            {"role": "user", "content": "ok"},
        ]
        await router.chat(messages)
        assert fake.last_model_used == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_thanks_in_deep_conversation_stays_primary(self):
        """'rahmat' in a conversation with many turns → primary."""
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, threshold=0.3)

        messages = []
        for i in range(6):
            messages.append({"role": "user", "content": f"question {i}"})
            messages.append({"role": "assistant", "content": f"answer {i}"})
        messages.append({"role": "user", "content": "rahmat"})

        await router.chat(messages)
        assert fake.last_model_used == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_simple_exchange_uses_cheap(self):
        """Simple greeting exchange → cheap model is fine."""
        fake = FakeProvider(model="claude-sonnet-4-6")
        router = RoutingProvider(fake, threshold=0.3)

        messages = [
            {"role": "user", "content": "salom"},
            {"role": "assistant", "content": "Salom! Qanday yordam bera olaman?"},
            {"role": "user", "content": "rahmat"},
        ]
        await router.chat(messages)
        assert fake.last_model_used == "claude-haiku-4-5-20251001"

    def test_assess_context_empty(self):
        assert RoutingProvider._assess_context([]) == 0.0

    def test_assess_context_single_message(self):
        assert RoutingProvider._assess_context([{"role": "user", "content": "hi"}]) == 0.0

    def test_assess_context_with_tool_result(self):
        messages = [
            {"role": "user", "content": "run tests"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "cmd", "input": {}}]},
            {"role": "tool", "content": "ok", "tool_use_id": "t1"},
            {"role": "assistant", "content": "Done"},
            {"role": "user", "content": "ha"},
        ]
        score = RoutingProvider._assess_context(messages)
        assert score >= 0.5


class TestRoutingStats:
    def test_savings_pct_zero_total(self):
        stats = RoutingStats()
        assert stats.savings_pct == 0.0

    def test_savings_pct_calculation(self):
        stats = RoutingStats(total=10, routed_cheap=7, routed_primary=3)
        assert stats.savings_pct == 70.0
