"""Tests for the lightweight quiz answer evaluator.

The evaluator's contract: take a PollRecord + user's option picks,
return a 3-4 sentence Uzbek evaluation. Internally it bypasses the
full agent loop and hits Haiku directly to avoid the rate-limit
blowup we saw in production when each poll answer fired a 100K-token
agent turn.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from qanot.poll_evaluator import (
    EVALUATOR_MAX_TOKENS,
    EVALUATOR_MODEL,
    PollEvaluator,
    _build_prompt,
)
from qanot.poll_state import PollRecord


# ────────── Fakes ──────────


class _FakeAnthropicClient:
    def __init__(self, *, reply="✅ TO'G'RI. ..."):
        self._reply = reply
        self.raise_on_create: Exception | None = None
        self.calls: list[dict] = []
        self.messages = self  # mirror anthropic.AsyncAnthropic().messages

    async def create(self, *, model, max_tokens, system, messages):
        self.calls.append({
            "model": model, "max_tokens": max_tokens,
            "system": system, "messages": messages,
        })
        if self.raise_on_create is not None:
            raise self.raise_on_create
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._reply)],
        )


class _FakeProvider:
    def __init__(self, *, reply="✅ TO'G'RI. ..."):
        self.client = _FakeAnthropicClient(reply=reply)


def _record(*, correct_ids=(1,), explanation="") -> PollRecord:
    return PollRecord(
        poll_id="p1", chat_id=1, thread_id=None,
        question="She ___ to school every day.",
        options=["go", "goes", "going", "gone"],
        correct_option_ids=list(correct_ids),
        sent_at=time.time(),
        message_id=42,
        explanation=explanation,
    )


def _run(coro):
    return asyncio.run(coro)


# ────────── Prompt construction ──────────


def test_prompt_includes_question_and_options():
    rec = _record()
    prompt = _build_prompt(rec, [1])
    assert "She ___" in prompt
    assert "A) go" in prompt
    assert "B) goes" in prompt
    assert "C) going" in prompt
    assert "D) gone" in prompt


def test_prompt_labels_correct_when_user_right():
    rec = _record(correct_ids=[1])
    prompt = _build_prompt(rec, [1])
    assert "TO'G'RI" in prompt
    assert "NOTO'G'RI" not in prompt


def test_prompt_labels_wrong_when_user_off():
    rec = _record(correct_ids=[1])
    prompt = _build_prompt(rec, [0])
    assert "NOTO'G'RI" in prompt


def test_prompt_includes_explanation_when_present():
    rec = _record(explanation="Third person singular: -s/-es.")
    prompt = _build_prompt(rec, [1])
    assert "Third person singular" in prompt


def test_prompt_omits_explanation_section_when_blank():
    rec = _record(explanation="")
    prompt = _build_prompt(rec, [1])
    assert "Izoh:" not in prompt


# ────────── Live evaluate() ──────────


def test_evaluate_calls_haiku_model():
    provider = _FakeProvider(reply="✅ TO'G'RI. Quvonchli xabar.")
    ev = PollEvaluator(provider)

    text = _run(ev.evaluate(_record(), [1]))
    assert text == "✅ TO'G'RI. Quvonchli xabar."

    call = provider.client.calls[-1]
    assert call["model"] == EVALUATOR_MODEL
    assert call["max_tokens"] == EVALUATOR_MAX_TOKENS


def test_evaluate_returns_text_block_content():
    provider = _FakeProvider(reply="❌ NOTO'G'RI. B to'g'ri.")
    ev = PollEvaluator(provider)
    text = _run(ev.evaluate(_record(), [0]))
    assert "NOTO'G'RI" in text


def test_evaluate_strips_whitespace():
    provider = _FakeProvider(reply="  ✅ TO'G'RI.  \n")
    ev = PollEvaluator(provider)
    text = _run(ev.evaluate(_record(), [1]))
    assert text == "✅ TO'G'RI."


# ────────── Fallback path ──────────


def test_provider_without_client_uses_fallback():
    """A provider that doesn't expose .client (older or mocked) must
    still produce ✅/❌ feedback via the deterministic fallback."""
    ev = PollEvaluator(provider=object())
    text = _run(ev.evaluate(_record(correct_ids=[1]), [1]))
    assert "TO'G'RI" in text


def test_api_error_falls_back():
    provider = _FakeProvider()
    provider.client.raise_on_create = RuntimeError("429 Rate limited")
    ev = PollEvaluator(provider)
    text = _run(ev.evaluate(_record(correct_ids=[1]), [0]))
    # Fallback marks the answer wrong + points at the right one.
    assert "NOTO'G'RI" in text
    assert "B) goes" in text


def test_empty_model_response_falls_back():
    provider = _FakeProvider(reply="")
    ev = PollEvaluator(provider)
    text = _run(ev.evaluate(_record(correct_ids=[1]), [1]))
    assert "TO'G'RI" in text  # fallback synthesised the message


def test_fallback_uses_explanation_when_available():
    provider = _FakeProvider()
    provider.client.raise_on_create = RuntimeError("network down")
    ev = PollEvaluator(provider)
    rec = _record(correct_ids=[1], explanation="Third person singular: -s.")
    text = _run(ev.evaluate(rec, [0]))
    assert "Third person singular" in text


def test_regular_poll_fallback_has_no_correctness_label():
    """Polls without correct_option_ids (regular polls, not quizzes)
    don't have a correct answer — fallback must just acknowledge the
    pick, not claim correctness."""
    provider = _FakeProvider()
    provider.client.raise_on_create = RuntimeError("down")
    ev = PollEvaluator(provider)
    rec = PollRecord(
        poll_id="p1", chat_id=1, thread_id=None,
        question="Sevimli rang?", options=["Ko'k", "Yashil"],
        correct_option_ids=[], sent_at=time.time(),
    )
    text = _run(ev.evaluate(rec, [0]))
    assert "TO'G'RI" not in text
    assert "NOTO'G'RI" not in text
    assert "Ko'k" in text


# ────────── Multi-correct quiz ──────────


def test_multi_correct_quiz_evaluation():
    """Bot API 9.6+ quizzes with multiple correct answers. The user
    must pick BOTH (in any order) to count as right."""
    rec = PollRecord(
        poll_id="p1", chat_id=1, thread_id=None,
        question="Which are Python data types?",
        options=["int", "stringo", "list", "dictomatic"],
        correct_option_ids=[0, 2], sent_at=time.time(),
    )
    provider = _FakeProvider()
    provider.client.raise_on_create = RuntimeError("down")  # force fallback
    ev = PollEvaluator(provider)
    # User picks BOTH correct options → correct
    text_right = _run(ev.evaluate(rec, [0, 2]))
    assert "TO'G'RI" in text_right
    # User picks just one of two → wrong
    text_wrong = _run(ev.evaluate(rec, [0]))
    assert "NOTO'G'RI" in text_wrong


# ────────── Prompt budget sanity ──────────


def test_prompt_is_small():
    """The whole reason for this module: each evaluation must be cheap.
    Build_prompt output should be well under 2KB for typical inputs so
    a quiz round doesn't burn rate limits like agent.run_turn does."""
    rec = _record(explanation="Third person singular requires -s/-es.")
    prompt = _build_prompt(rec, [1])
    assert len(prompt) < 2000
