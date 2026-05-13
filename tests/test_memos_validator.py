"""Tests for qanot.memos.validator — Haiku-backed draft-against-rules check."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qanot.memos import MemoSpec, MemoType, ValidationResult, validate_text_against_memos


def _run(coro):
    return asyncio.run(coro)


def _rule(name: str, description: str, body: str, *, how: str = "") -> MemoSpec:
    return MemoSpec(
        name=name, description=description, type=MemoType.FEEDBACK,
        body=body, path=Path(f"/tmp/{name}.md"), how_to_apply=how,
    )


def _user_memo(name: str, body: str) -> MemoSpec:
    return MemoSpec(
        name=name, description="user fact", type=MemoType.USER,
        body=body, path=Path(f"/tmp/{name}.md"),
    )


class StubMessage:
    def __init__(self, text: str):
        self.content = [type("Block", (), {"text": text})()]


class StubClient:
    def __init__(self, response_text: str = "", *, raise_exc=None):
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.last_call: dict | None = None
        self.call_count = 0

        outer = self

        class _Messages:
            async def create(self, **kwargs):
                outer.call_count += 1
                outer.last_call = kwargs
                if outer.raise_exc is not None:
                    raise outer.raise_exc
                return StubMessage(outer.response_text)

        self.messages = _Messages()


# ─── compliant path ──────────────────────────────────────────────


class TestCompliant:
    def test_passes_through(self):
        rule = _rule(
            "feedback-title-format",
            "Title must be D-month, YYYY",
            "ALWAYS: 13-may, 2026. NEVER: Daily Entry — YYYY",
        )
        client = StubClient(json.dumps({
            "compliant": True,
            "verified": "13-may, 2026",
        }))
        result = _run(validate_text_against_memos(
            "13-may, 2026", field_context="Notion title",
            active_memos=[rule], client=client,
        ))
        assert isinstance(result, ValidationResult)
        assert result.was_changed is False
        assert result.verified == "13-may, 2026"
        assert result.violations == []


# ─── violation path ──────────────────────────────────────────────


class TestRewrite:
    def test_violation_rewritten(self):
        rule = _rule(
            "feedback-title-format", "Title must be D-month, YYYY",
            "ALWAYS use \"13-may, 2026\". NEVER write \"Daily Entry — YYYY-yil\".",
        )
        client = StubClient(json.dumps({
            "compliant": False,
            "verified": "13-may, 2026",
            "violations": [
                "feedback-title-format: had English 'Daily Entry —' prefix",
            ],
        }))
        result = _run(validate_text_against_memos(
            "Daily Entry — 2026-yil, 13-may (Chorshanba)",
            field_context="Notion title",
            active_memos=[rule], client=client,
        ))
        assert result.was_changed is True
        assert result.verified == "13-may, 2026"
        assert len(result.violations) == 1
        assert "Daily Entry" in result.violations[0]

    def test_summary_line_reports_changes(self):
        result = ValidationResult(
            original="x", verified="y", was_changed=True,
            violations=["a", "b"],
        )
        assert "rewrote" in result.summary_line()
        assert "2 violation" in result.summary_line()


# ─── filtering ──────────────────────────────────────────────────


class TestRuleFilter:
    def test_only_feedback_rules_consulted(self):
        feedback = _rule("fb-x", "rule x", "rule body")
        user_memo = _user_memo("u-x", "user fact body")
        # Even with 2 memos, validator should only consult the feedback one.
        # Because both memos exist, the LLM IS called. We assert that the
        # user_block sent to LLM only mentions the feedback rule.
        client = StubClient(json.dumps({"compliant": True, "verified": "x"}))
        _run(validate_text_against_memos(
            "draft", field_context="ctx",
            active_memos=[feedback, user_memo], client=client,
        ))
        user_msg = client.last_call["messages"][0]["content"]
        assert "fb-x" in user_msg
        assert "u-x" not in user_msg

    def test_no_feedback_rules_short_circuits(self):
        # Only user-type memos in scope → no LLM call needed.
        user_memo = _user_memo("u-x", "user fact")
        client = StubClient("never called")
        result = _run(validate_text_against_memos(
            "draft", field_context="ctx",
            active_memos=[user_memo], client=client,
        ))
        assert result.was_changed is False
        assert result.verified == "draft"
        assert client.call_count == 0

    def test_empty_active_memos(self):
        client = StubClient("never called")
        result = _run(validate_text_against_memos(
            "draft", field_context="ctx",
            active_memos=[], client=client,
        ))
        assert client.call_count == 0
        assert result.was_changed is False


# ─── failure modes ──────────────────────────────────────────────


class TestFailureModes:
    def test_empty_text_short_circuits(self):
        client = StubClient("never called")
        result = _run(validate_text_against_memos(
            "", field_context="ctx",
            active_memos=[_rule("r", "d", "b")], client=client,
        ))
        assert client.call_count == 0
        assert result.was_changed is False

    def test_llm_failure_passes_through(self):
        client = StubClient(raise_exc=RuntimeError("api down"))
        result = _run(validate_text_against_memos(
            "draft", field_context="ctx",
            active_memos=[_rule("r", "d", "b")], client=client,
        ))
        assert result.was_changed is False
        assert result.verified == "draft"

    def test_unparseable_response_passes_through(self):
        client = StubClient("not json at all")
        result = _run(validate_text_against_memos(
            "draft", field_context="ctx",
            active_memos=[_rule("r", "d", "b")], client=client,
        ))
        assert result.was_changed is False

    def test_empty_verified_field_passes_through(self):
        # Defensive: if LLM emits compliant=false but empty `verified`,
        # we don't blank the user's text.
        client = StubClient(json.dumps({
            "compliant": False, "verified": "", "violations": ["x"],
        }))
        result = _run(validate_text_against_memos(
            "draft", field_context="ctx",
            active_memos=[_rule("r", "d", "b")], client=client,
        ))
        assert result.was_changed is False
        assert result.verified == "draft"


# ─── parser tolerance ───────────────────────────────────────────


class TestParserTolerance:
    def test_code_fences(self):
        wrapped = (
            "```json\n"
            + json.dumps({"compliant": True, "verified": "draft"})
            + "\n```"
        )
        client = StubClient(wrapped)
        result = _run(validate_text_against_memos(
            "draft", field_context="ctx",
            active_memos=[_rule("r", "d", "b")], client=client,
        ))
        assert result.verified == "draft"

    def test_violations_string_coerced_to_list(self):
        # LLM occasionally emits violations as a string instead of array.
        client = StubClient(json.dumps({
            "compliant": False, "verified": "fixed",
            "violations": "single violation",
        }))
        result = _run(validate_text_against_memos(
            "draft", field_context="ctx",
            active_memos=[_rule("r", "d", "b")], client=client,
        ))
        assert result.violations == ["single violation"]


# ─── prompt shape ───────────────────────────────────────────────


class TestPromptShape:
    def test_field_context_included(self):
        client = StubClient(json.dumps({"compliant": True, "verified": "draft"}))
        _run(validate_text_against_memos(
            "draft", field_context="Notion page title",
            active_memos=[_rule("r", "d", "b")], client=client,
        ))
        user_msg = client.last_call["messages"][0]["content"]
        assert "Notion page title" in user_msg

    def test_how_to_apply_passed_to_llm(self):
        rule = _rule(
            "feedback-x", "rule desc", "rule body",
            how="When writing Notion daily-note titles only.",
        )
        client = StubClient(json.dumps({"compliant": True, "verified": "x"}))
        _run(validate_text_against_memos(
            "x", field_context="ctx",
            active_memos=[rule], client=client,
        ))
        user_msg = client.last_call["messages"][0]["content"]
        assert "Notion daily-note titles" in user_msg

    def test_system_block_constant(self):
        # Stable system block = prompt cache stays warm.
        client = StubClient(json.dumps({"compliant": True, "verified": "x"}))
        _run(validate_text_against_memos(
            "x", field_context="ctx",
            active_memos=[_rule("r", "d", "b")], client=client,
        ))
        sys1 = client.last_call["system"]
        _run(validate_text_against_memos(
            "y", field_context="other",
            active_memos=[_rule("r2", "d", "b")], client=client,
        ))
        sys2 = client.last_call["system"]
        assert sys1 == sys2
