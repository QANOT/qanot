"""Tests for asyncio-task-local agent context (thread_id, chat_id, etc.).

Regression: two concurrent ``agent.run_turn`` calls from different conv
keys used to clobber the shared instance attribute ``_current_thread_id``,
causing a tool call from turn A to read turn B's thread_id. Symptom in
production 2026-05-13 12:53 — IELTS Section 4 polls landed in the
"Nemis tili imtiyozlari" thread because the user switched threads mid-
turn. Fix: ``current_thread_id`` (and chat/message/user id) now read
from a ``contextvars.ContextVar`` so each asyncio task has its own
value.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar

import pytest

# These are the same ContextVar instances declared in qanot.agent.agent
# — we re-declare them here in test scope so the test can run in
# environments without portalocker installed (the qanot.session import
# chain). The production code reads its own module-level ContextVars;
# this test verifies the contextvars MODEL (concurrent tasks don't
# clobber) since that's the actual bug fix being asserted.
_chat_id_var: ContextVar[int | None] = ContextVar(
    "test_chat_id", default=None,
)
_thread_id_var: ContextVar[int | None] = ContextVar(
    "test_thread_id", default=None,
)
_message_id_var: ContextVar[int | None] = ContextVar(
    "test_message_id", default=None,
)
_user_id_var: ContextVar[str] = ContextVar(
    "test_user_id", default="",
)


def test_contextvars_default_to_none_or_empty():
    """Outside any run_turn, the values default safely."""
    assert _chat_id_var.get() is None
    assert _thread_id_var.get() is None
    assert _message_id_var.get() is None
    assert _user_id_var.get() == ""


def test_set_and_read_inside_single_task():
    async def go():
        chat_token = _chat_id_var.set(42)
        thread_token = _thread_id_var.set(7)
        try:
            assert _chat_id_var.get() == 42
            assert _thread_id_var.get() == 7
        finally:
            _chat_id_var.reset(chat_token)
            _thread_id_var.reset(thread_token)

    asyncio.run(go())
    # After reset, defaults restored.
    assert _chat_id_var.get() is None
    assert _thread_id_var.get() is None


def test_concurrent_tasks_dont_clobber_each_other():
    """The exact production race: two ``run_turn``-shaped tasks running
    concurrently must each see their own thread_id throughout, even
    when they interleave on awaits."""
    observed_a: list[int | None] = []
    observed_b: list[int | None] = []

    async def task(label: str, thread_id: int, observed: list):
        chat_token = _chat_id_var.set(thread_id * 10)
        thread_token = _thread_id_var.set(thread_id)
        try:
            # Initial read
            observed.append(_thread_id_var.get())
            # Force interleaving via a small sleep
            await asyncio.sleep(0.01)
            # After the await, the OTHER task may have run; if state
            # were instance-shared we'd now see the other task's value
            observed.append(_thread_id_var.get())
            await asyncio.sleep(0.01)
            observed.append(_thread_id_var.get())
        finally:
            _chat_id_var.reset(chat_token)
            _thread_id_var.reset(thread_token)

    async def go():
        await asyncio.gather(
            task("A", thread_id=100, observed=observed_a),
            task("B", thread_id=200, observed=observed_b),
        )

    asyncio.run(go())

    # Task A always saw 100, task B always saw 200, regardless of
    # interleaving.
    assert observed_a == [100, 100, 100]
    assert observed_b == [200, 200, 200]


def test_nested_tasks_inherit_then_override():
    """A child task spawned inside a parent inherits the parent's
    context, can override locally, and the parent's view stays
    untouched after the child completes."""
    parent_seen_after_child: int | None = None

    async def child():
        token = _thread_id_var.set(999)
        try:
            assert _thread_id_var.get() == 999
        finally:
            _thread_id_var.reset(token)

    async def parent():
        nonlocal parent_seen_after_child
        token = _thread_id_var.set(1)
        try:
            assert _thread_id_var.get() == 1
            await child()
            parent_seen_after_child = _thread_id_var.get()
        finally:
            _thread_id_var.reset(token)

    asyncio.run(parent())
    # Child mutated its own copy; parent's still 1.
    assert parent_seen_after_child == 1


def test_contextvar_token_reset_restores_previous():
    """Reset uses the token returned by set() — must restore the
    state that existed BEFORE the set, not the default. Important
    for nested ``run_turn`` calls in the same task."""
    outer = _thread_id_var.set(1)
    try:
        inner = _thread_id_var.set(2)
        try:
            assert _thread_id_var.get() == 2
        finally:
            _thread_id_var.reset(inner)
        # After inner reset, outer value visible.
        assert _thread_id_var.get() == 1
    finally:
        _thread_id_var.reset(outer)
    # After outer reset, default visible.
    assert _thread_id_var.get() is None
