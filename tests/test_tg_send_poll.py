"""Tests for the tg_send_poll tool — native Telegram polls."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from qanot.context import ContextTracker
from qanot.registry import ToolRegistry
from qanot.tools.builtin import register_builtin_tools


# ────────── Test scaffolding ──────────


def _make_registry(*, bot=None, chat_id=42, thread_id=None, tmp_path=None):
    """Build a ToolRegistry with the poll tool registered and the
    requested bot/chat/thread stubbing."""
    workspace = str(tmp_path) if tmp_path else "/tmp/qanot_test_ws"
    reg = ToolRegistry()
    ctx = ContextTracker()
    register_builtin_tools(
        reg, workspace, ctx,
        get_bot=lambda: bot,
        get_chat_id=lambda: chat_id,
        get_thread_id=lambda: thread_id,
    )
    return reg


def _make_bot():
    """A bot mock whose __call__ records the SendPoll method instance."""
    bot = AsyncMock()
    # The bot is invoked as bot(SendPoll(...)) — async __call__ returns
    # a Message-like object with .message_id.
    bot.return_value = SimpleNamespace(message_id=12345)
    return bot


def _run(coro):
    return asyncio.run(coro)


# ────────── Validation ──────────


def test_missing_question(tmp_path):
    """Required-parameter validation is enforced by ToolRegistry before
    the handler runs — the error mentions the missing field by name."""
    reg = _make_registry(tmp_path=tmp_path, bot=_make_bot())
    out = _run(reg.execute("tg_send_poll", {"options": ["A", "B"]}))
    assert "question" in out
    assert "Missing required" in out or "is required" in out


def test_too_few_options(tmp_path):
    reg = _make_registry(tmp_path=tmp_path, bot=_make_bot())
    out = _run(reg.execute("tg_send_poll", {
        "question": "Q?", "options": ["only one"],
    }))
    assert "2-10" in out


def test_too_many_options(tmp_path):
    reg = _make_registry(tmp_path=tmp_path, bot=_make_bot())
    out = _run(reg.execute("tg_send_poll", {
        "question": "Q?", "options": [f"opt{i}" for i in range(11)],
    }))
    assert "2-10" in out


def test_question_too_long(tmp_path):
    reg = _make_registry(tmp_path=tmp_path, bot=_make_bot())
    out = _run(reg.execute("tg_send_poll", {
        "question": "x" * 301, "options": ["A", "B"],
    }))
    assert "too long" in out


def test_option_too_long(tmp_path):
    reg = _make_registry(tmp_path=tmp_path, bot=_make_bot())
    out = _run(reg.execute("tg_send_poll", {
        "question": "Q?", "options": ["A", "x" * 101],
    }))
    assert "too long" in out


def test_correct_option_id_out_of_range(tmp_path):
    reg = _make_registry(tmp_path=tmp_path, bot=_make_bot())
    out = _run(reg.execute("tg_send_poll", {
        "question": "Q?", "options": ["A", "B"],
        "correct_option_id": 5,
    }))
    assert "out of range" in out


def test_no_bot_available(tmp_path):
    reg = _make_registry(tmp_path=tmp_path, bot=None)
    out = _run(reg.execute("tg_send_poll", {
        "question": "Q?", "options": ["A", "B"],
    }))
    assert "not available" in out


# ────────── Regular poll happy path ──────────


def test_regular_poll_basic(tmp_path):
    bot = _make_bot()
    reg = _make_registry(tmp_path=tmp_path, bot=bot)
    out = _run(reg.execute("tg_send_poll", {
        "question": "Sevimli rang?",
        "options": ["Ko'k", "Yashil", "Qizil"],
    }))
    result = json.loads(out)
    assert result["success"] is True
    assert result["poll_type"] == "regular"
    assert result["option_count"] == 3
    assert result["message_id"] == 12345

    # Verify the actual SendPoll method instance passed to bot()
    sent_method = bot.call_args[0][0]
    assert sent_method.question == "Sevimli rang?"
    assert sent_method.options == ["Ko'k", "Yashil", "Qizil"]
    assert sent_method.type is None or sent_method.type == "regular"
    # No thread when not set
    assert sent_method.message_thread_id is None


def test_regular_poll_with_thread_id(tmp_path):
    bot = _make_bot()
    reg = _make_registry(tmp_path=tmp_path, bot=bot, thread_id=789)
    _run(reg.execute("tg_send_poll", {
        "question": "Q?", "options": ["A", "B"],
    }))
    sent_method = bot.call_args[0][0]
    assert sent_method.message_thread_id == 789


# ────────── Quiz mode ──────────


def test_quiz_with_single_correct(tmp_path):
    bot = _make_bot()
    reg = _make_registry(tmp_path=tmp_path, bot=bot)
    out = _run(reg.execute("tg_send_poll", {
        "question": "She ___ to school every day.",
        "options": ["go", "goes", "going", "gone"],
        "correct_option_id": 1,
        "explanation": "Third person singular → 'goes'.",
    }))
    result = json.loads(out)
    assert result["poll_type"] == "quiz"

    sent = bot.call_args[0][0]
    assert sent.type == "quiz"
    assert sent.correct_option_id == 1
    assert "goes" in sent.explanation


def test_quiz_with_multi_correct(tmp_path):
    """Bot API 9.6+: multi-correct quiz via correct_option_ids list."""
    bot = _make_bot()
    reg = _make_registry(tmp_path=tmp_path, bot=bot)
    _run(reg.execute("tg_send_poll", {
        "question": "Which are valid Python data types?",
        "options": ["int", "stringo", "list", "dictomatic"],
        "correct_option_ids": [0, 2],
    }))
    sent = bot.call_args[0][0]
    assert sent.type == "quiz"
    assert sent.correct_option_ids == [0, 2]


def test_quiz_explanation_truncated_to_200_chars(tmp_path):
    bot = _make_bot()
    reg = _make_registry(tmp_path=tmp_path, bot=bot)
    _run(reg.execute("tg_send_poll", {
        "question": "Q?",
        "options": ["A", "B"],
        "correct_option_id": 0,
        "explanation": "x" * 500,
    }))
    sent = bot.call_args[0][0]
    assert len(sent.explanation) == 200


# ────────── Anonymity / multi-answer ──────────


def test_non_anonymous_poll(tmp_path):
    bot = _make_bot()
    reg = _make_registry(tmp_path=tmp_path, bot=bot)
    _run(reg.execute("tg_send_poll", {
        "question": "Q?", "options": ["A", "B"],
        "is_anonymous": False,
    }))
    sent = bot.call_args[0][0]
    assert sent.is_anonymous is False


def test_allows_multiple_answers_in_regular(tmp_path):
    bot = _make_bot()
    reg = _make_registry(tmp_path=tmp_path, bot=bot)
    _run(reg.execute("tg_send_poll", {
        "question": "Sizga qaysi sport yoqadi?",
        "options": ["Football", "Basketball", "Tennis"],
        "allows_multiple_answers": True,
    }))
    sent = bot.call_args[0][0]
    assert sent.allows_multiple_answers is True


def test_allows_multiple_answers_ignored_in_quiz(tmp_path):
    """allows_multiple_answers is invalid alongside type=quiz — we
    silently drop it to match Telegram's behaviour."""
    bot = _make_bot()
    reg = _make_registry(tmp_path=tmp_path, bot=bot)
    _run(reg.execute("tg_send_poll", {
        "question": "Q?", "options": ["A", "B"],
        "correct_option_id": 0,
        "allows_multiple_answers": True,
    }))
    sent = bot.call_args[0][0]
    # Quiz polls don't accept multi-answer; we must not have set it.
    assert (
        sent.allows_multiple_answers is None
        or sent.allows_multiple_answers is False
    )


# ────────── Timed polls ──────────


def test_open_period_valid_range(tmp_path):
    bot = _make_bot()
    reg = _make_registry(tmp_path=tmp_path, bot=bot)
    _run(reg.execute("tg_send_poll", {
        "question": "Q?", "options": ["A", "B"],
        "open_period": 60,
    }))
    sent = bot.call_args[0][0]
    assert sent.open_period == 60


def test_open_period_out_of_range_ignored(tmp_path):
    """Out-of-range open_period is silently dropped (defensive against
    bad model output) rather than erroring — the poll still gets sent."""
    bot = _make_bot()
    reg = _make_registry(tmp_path=tmp_path, bot=bot)
    _run(reg.execute("tg_send_poll", {
        "question": "Q?", "options": ["A", "B"],
        "open_period": 999_999,
    }))
    sent = bot.call_args[0][0]
    assert sent.open_period is None


# ────────── Error propagation ──────────


def test_send_poll_failure_returns_error(tmp_path):
    bot = AsyncMock()
    bot.side_effect = RuntimeError("Bad Request: chat not found")
    reg = _make_registry(tmp_path=tmp_path, bot=bot)
    out = _run(reg.execute("tg_send_poll", {
        "question": "Q?", "options": ["A", "B"],
    }))
    assert "Telegram sendPoll failed" in out
    assert "chat not found" in out
