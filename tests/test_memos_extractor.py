"""Tests for qanot.memos.extractor — Haiku-backed memo extraction.

We never call a real LLM in tests. Instead a stub client returns
canned JSON responses so we exercise the parser, validator, and
fallback paths deterministically.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from qanot.memos import ExtractedMemo, MemoType, extract_memo


def _run(coro):
    return asyncio.run(coro)


class StubMessage:
    """Mimics anthropic SDK's response shape (content[0].text)."""

    def __init__(self, text: str):
        self.content = [type("Block", (), {"text": text})()]


class StubClient:
    """Stub for ``anthropic.AsyncAnthropic`` — only implements messages.create."""

    def __init__(self, response_text: str = "", *, raise_exc: Exception | None = None):
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.last_call: dict | None = None
        self.call_count = 0

        # Expose .messages.create — async — matching the real client surface.
        outer = self

        class _Messages:
            async def create(self, **kwargs):
                outer.call_count += 1
                outer.last_call = kwargs
                if outer.raise_exc is not None:
                    raise outer.raise_exc
                return StubMessage(outer.response_text)

        self.messages = _Messages()


# ─── happy path: should_save=true ────────────────────────────────


class TestSavePaths:
    def test_full_payload(self):
        client = StubClient(json.dumps({
            "should_save": True,
            "name": "feedback-title-format",
            "description": "Daily note titles must use D-month, YYYY",
            "type": "feedback",
            "body": "ALWAYS use \"13-may, 2026\". NEVER write \"Daily Entry — YYYY-yil\".",
            "user_scope": "1545224574",
            "thread_scope": "kunlik-yozuv",
            "why": "User stated this rule on 2026-05-13",
            "how_to_apply": "When writing daily Notion titles.",
        }))
        out = _run(extract_memo(
            client, "sarlavha har doim 13-may, 2026 ko'rinishida eslab qol",
            user_id="1545224574", thread_id="kunlik-yozuv",
        ))
        assert out is not None
        assert isinstance(out, ExtractedMemo)
        assert out.name == "feedback-title-format"
        assert out.type == MemoType.FEEDBACK
        assert out.user_scope == "1545224574"
        assert out.thread_scope == "kunlik-yozuv"
        assert "Daily Entry" in out.body

    def test_global_memo(self):
        client = StubClient(json.dumps({
            "should_save": True,
            "name": "user-language",
            "description": "Bot communicates in Uzbek by default",
            "type": "user",
            "body": "Default language: Uzbek (Latin script).",
            "user_scope": "",
            "thread_scope": "",
        }))
        out = _run(extract_memo(client, "har doim o'zbekcha gaplash"))
        assert out is not None
        assert out.user_scope == ""
        assert out.thread_scope == ""

    def test_to_kwargs_round_trip(self):
        client = StubClient(json.dumps({
            "should_save": True,
            "name": "user-color",
            "description": "Favorite color blue",
            "type": "user",
            "body": "User likes blue (ko'k)",
        }))
        out = _run(extract_memo(client, "men ko'k rangni yoqtiraman"))
        assert out is not None
        kwargs = out.to_kwargs()
        assert kwargs["name"] == "user-color"
        assert kwargs["memo_type"] == MemoType.USER


# ─── skip paths ──────────────────────────────────────────────────


class TestSkipPaths:
    def test_should_save_false(self):
        client = StubClient(json.dumps({
            "should_save": False,
            "reason": "one-off request, not a persistent rule",
        }))
        out = _run(extract_memo(client, "create today's report"))
        assert out is None

    def test_empty_message(self):
        client = StubClient("anything")
        out = _run(extract_memo(client, ""))
        assert out is None
        # We short-circuited before calling the LLM.
        assert client.call_count == 0

    def test_whitespace_only(self):
        client = StubClient("anything")
        out = _run(extract_memo(client, "   \n\t  "))
        assert out is None
        assert client.call_count == 0


# ─── error / failure modes ──────────────────────────────────────


class TestErrorPaths:
    def test_llm_call_failure_returns_none(self):
        client = StubClient(raise_exc=RuntimeError("api down"))
        out = _run(extract_memo(client, "har doim X format"))
        assert out is None  # logged warning, no exception

    def test_unparseable_response(self):
        client = StubClient("this is not JSON")
        out = _run(extract_memo(client, "har doim X format"))
        assert out is None

    def test_partial_response_missing_required_field(self):
        # No `body` field → validator raises → extractor returns None.
        client = StubClient(json.dumps({
            "should_save": True,
            "name": "x",
            "description": "y",
            "type": "user",
            # body missing
        }))
        out = _run(extract_memo(client, "anything"))
        assert out is None

    def test_invalid_type_rejected(self):
        client = StubClient(json.dumps({
            "should_save": True,
            "name": "x",
            "description": "y",
            "type": "wrong-type",
            "body": "b",
        }))
        out = _run(extract_memo(client, "anything"))
        assert out is None


# ─── JSON fences tolerated ──────────────────────────────────────


class TestParserTolerance:
    def test_code_fences(self):
        wrapped = (
            "```json\n"
            + json.dumps({
                "should_save": True, "name": "x", "description": "d",
                "type": "user", "body": "b",
            })
            + "\n```"
        )
        client = StubClient(wrapped)
        out = _run(extract_memo(client, "har doim X"))
        assert out is not None
        assert out.name == "x"

    def test_prose_around_json(self):
        # LLM occasionally emits a brief preamble despite the prompt.
        wrapped = (
            "Here's the result:\n"
            + json.dumps({
                "should_save": True, "name": "x", "description": "d",
                "type": "user", "body": "b",
            })
        )
        client = StubClient(wrapped)
        out = _run(extract_memo(client, "har doim X"))
        assert out is not None


# ─── scope coercion ─────────────────────────────────────────────


class TestScopeCoercion:
    def test_numeric_user_scope_kept_as_string(self):
        # LLM might emit user_scope as a JSON number — we coerce to str.
        payload = (
            '{"should_save": true, "name": "x", "description": "d", '
            '"type": "user", "body": "b", "user_scope": 1545224574}'
        )
        client = StubClient(payload)
        out = _run(extract_memo(client, "any", user_id="1545224574"))
        assert out is not None
        assert out.user_scope == "1545224574"
        assert isinstance(out.user_scope, str)

    def test_null_scope_becomes_empty_string(self):
        payload = (
            '{"should_save": true, "name": "x", "description": "d", '
            '"type": "user", "body": "b", "user_scope": null}'
        )
        client = StubClient(payload)
        out = _run(extract_memo(client, "any"))
        assert out is not None
        assert out.user_scope == ""


# ─── prompt construction ────────────────────────────────────────


class TestPromptShape:
    def test_caller_context_included(self):
        client = StubClient(json.dumps({"should_save": False, "reason": "x"}))
        _run(extract_memo(
            client, "test", user_id="alice", thread_id="t1",
            today_iso="2026-05-14",
        ))
        # We can inspect what was passed.
        call = client.last_call
        assert call["model"]
        user_msg = call["messages"][0]["content"]
        assert "alice" in user_msg
        assert "t1" in user_msg
        assert "2026-05-14" in user_msg
        assert "test" in user_msg

    def test_system_block_constant(self):
        # System prompt should be identical across calls so prompt-cache
        # stays warm. Just check it's non-empty and contains the skip-rules.
        client = StubClient(json.dumps({"should_save": False, "reason": "x"}))
        _run(extract_memo(client, "msg1"))
        sys1 = client.last_call["system"]
        _run(extract_memo(client, "msg2"))
        sys2 = client.last_call["system"]
        assert sys1 == sys2
        assert "SAVE when" in sys1
        assert "SKIP these" in sys1
