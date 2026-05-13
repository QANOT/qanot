"""Tests for the conversational-poll registry.

The registry's job is to remember every poll the bot sent so that when
Telegram fires a ``poll_answer`` update (which only includes the poll
id + user vote) we can route the answer back to the right chat, thread,
and quiz context. Without persistence we'd lose every pending poll on
restart, which would break long quiz sessions.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from qanot.poll_state import (
    POLL_STATE_FILENAME,
    POLL_TTL_SECONDS,
    PollRecord,
    PollRegistry,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def workspace(tmp_path):
    return str(tmp_path)


# ────────── Register + get ──────────


def test_register_then_get(workspace):
    reg = PollRegistry(workspace)

    async def go():
        await reg.register(
            poll_id="p1", chat_id=42, thread_id=None,
            question="Q?", options=["A", "B"], correct_option_ids=[0],
        )

    _run(go())
    rec = reg.get("p1")
    assert rec is not None
    assert rec.chat_id == 42
    assert rec.options == ["A", "B"]
    assert rec.correct_option_ids == [0]


def test_get_unknown_returns_none(workspace):
    reg = PollRegistry(workspace)
    assert reg.get("not-a-real-poll") is None


# ────────── Persistence ──────────


def test_state_written_to_disk(workspace):
    reg = PollRegistry(workspace)

    async def go():
        await reg.register(
            poll_id="p1", chat_id=42, thread_id=7,
            question="Q?", options=["A", "B"], correct_option_ids=[1],
            explanation="Because B.",
        )

    _run(go())
    state_path = Path(workspace) / POLL_STATE_FILENAME
    assert state_path.exists()
    data = json.loads(state_path.read_text())
    assert "p1" in data
    assert data["p1"]["chat_id"] == 42
    assert data["p1"]["thread_id"] == 7


def test_state_survives_restart(workspace):
    reg1 = PollRegistry(workspace)

    async def first():
        await reg1.register(
            poll_id="p1", chat_id=42, thread_id=None,
            question="Q?", options=["A", "B"], correct_option_ids=[0],
        )

    _run(first())

    # Fresh instance same workspace — must see the same record.
    reg2 = PollRegistry(workspace)
    rec = reg2.get("p1")
    assert rec is not None
    assert rec.chat_id == 42


def test_corrupt_state_file_ignored(workspace):
    (Path(workspace) / POLL_STATE_FILENAME).write_text("not json")
    reg = PollRegistry(workspace)
    assert reg.get("anything") is None


def test_stale_entries_evicted_on_load(workspace):
    """Entries older than TTL are dropped on construction — no need to
    wait for the explicit sweep."""
    state_path = Path(workspace) / POLL_STATE_FILENAME
    long_ago = time.time() - POLL_TTL_SECONDS - 100
    state_path.write_text(json.dumps({
        "old": {
            "poll_id": "old", "chat_id": 1, "thread_id": None,
            "question": "Q", "options": ["A", "B"],
            "correct_option_ids": [0], "sent_at": long_ago,
        },
        "fresh": {
            "poll_id": "fresh", "chat_id": 1, "thread_id": None,
            "question": "Q", "options": ["A", "B"],
            "correct_option_ids": [0], "sent_at": time.time(),
        },
    }))
    reg = PollRegistry(workspace)
    assert reg.get("old") is None
    assert reg.get("fresh") is not None


# ────────── Answer recording ──────────


def test_record_answer_first_time_returns_true(workspace):
    reg = PollRegistry(workspace)

    async def go():
        await reg.register(
            poll_id="p1", chat_id=42, thread_id=None,
            question="Q?", options=["A", "B", "C"], correct_option_ids=[1],
        )
        first = await reg.record_answer("p1", user_id=100, option_ids=[1])
        return first

    assert _run(go()) is True


def test_record_answer_same_options_is_noop(workspace):
    """Revoting with the same options must NOT re-fire the agent turn —
    record_answer returns False so the caller can skip."""
    reg = PollRegistry(workspace)

    async def go():
        await reg.register(
            poll_id="p1", chat_id=42, thread_id=None,
            question="Q?", options=["A", "B"], correct_option_ids=[0],
        )
        first = await reg.record_answer("p1", user_id=100, option_ids=[0])
        second = await reg.record_answer("p1", user_id=100, option_ids=[0])
        return first, second

    first, second = _run(go())
    assert first is True
    assert second is False


def test_revote_with_different_options_returns_true(workspace):
    """Telegram lets users change their answer in some poll types — a
    real revote IS a new answer and should fire a fresh agent turn."""
    reg = PollRegistry(workspace)

    async def go():
        await reg.register(
            poll_id="p1", chat_id=42, thread_id=None,
            question="Q?", options=["A", "B", "C"], correct_option_ids=[1],
        )
        await reg.record_answer("p1", user_id=100, option_ids=[0])
        revote = await reg.record_answer("p1", user_id=100, option_ids=[2])
        return revote

    assert _run(go()) is True


def test_record_answer_for_unknown_poll_returns_false(workspace):
    """An answer for a poll we never registered (TTL'd out, or another
    bot's poll arriving on our handler) is a graceful no-op."""
    reg = PollRegistry(workspace)
    assert _run(reg.record_answer("ghost", user_id=1, option_ids=[0])) is False


# ────────── TTL sweep ──────────


def test_evict_stale_removes_old_polls(workspace):
    reg = PollRegistry(workspace)

    async def go():
        await reg.register(
            poll_id="p1", chat_id=42, thread_id=None,
            question="Q?", options=["A", "B"], correct_option_ids=[0],
        )
        # Backdate the in-memory record past the TTL boundary.
        reg._polls["p1"].sent_at = time.time() - POLL_TTL_SECONDS - 100
        evicted = await reg.evict_stale()
        return evicted

    assert _run(go()) == 1
    assert reg.get("p1") is None


# ────────── Synthetic message construction ──────────


def _make_record(*, correct_ids=(1,), explanation="") -> PollRecord:
    return PollRecord(
        poll_id="p", chat_id=1, thread_id=None,
        question="She ___ to school every day.",
        options=["go", "goes", "going", "gone"],
        correct_option_ids=list(correct_ids),
        sent_at=time.time(),
        explanation=explanation,
    )


def test_synthetic_message_marks_correct():
    rec = _make_record(correct_ids=[1])
    msg = PollRegistry.build_answer_message(rec, [1])
    assert "TO'G'RI" in msg
    assert "B) goes" in msg
    assert "She ___" in msg


def test_synthetic_message_marks_wrong():
    rec = _make_record(correct_ids=[1])
    msg = PollRegistry.build_answer_message(rec, [0])
    assert "NOTO'G'RI" in msg
    assert "A) go" in msg  # what user picked
    assert "B) goes" in msg  # what was correct


def test_synthetic_message_includes_explanation():
    rec = _make_record(correct_ids=[1], explanation="Third person singular.")
    msg = PollRegistry.build_answer_message(rec, [1])
    assert "Third person singular." in msg


def test_synthetic_message_for_regular_poll_omits_correctness():
    """Non-quiz polls have no 'correct' answer — the message must not
    claim one. We just acknowledge the user's pick."""
    rec = PollRecord(
        poll_id="p", chat_id=1, thread_id=None,
        question="Sevimli rang?",
        options=["Ko'k", "Yashil"],
        correct_option_ids=[],
        sent_at=time.time(),
    )
    msg = PollRegistry.build_answer_message(rec, [0])
    assert "TO'G'RI" not in msg
    assert "NOTO'G'RI" not in msg
    assert "Ko'k" in msg
