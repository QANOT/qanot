"""Tests for ``thread_titler._save_thread_context_memo``.

The titler's existing test suite covers Telegram-side title application.
This new file covers the memo-write step added on 2026-05-14: after a
successful title rename, persist a ``project-thread-<id>.md`` memo so
the topic survives in-memory eviction.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from qanot.memos import MemoStore, MemoType
from qanot.thread_titler import ThreadTitler


def _run(coro):
    return asyncio.run(coro)


def _make_titler(tmp_path: Path) -> ThreadTitler:
    """Construct a ThreadTitler with stub bot + provider, real workspace."""
    titler = ThreadTitler(
        bot=MagicMock(),
        provider=MagicMock(client=None),  # we don't call _generate_title
        workspace_dir=str(tmp_path),
    )
    return titler


# ─── memo save path ─────────────────────────────────────────────


class TestSaveThreadContextMemo:
    def test_basic_save(self, tmp_path):
        titler = _make_titler(tmp_path)
        _run(titler._save_thread_context_memo(
            chat_id=1545224574,
            thread_id=212432,
            title="YouTube kontenti rejasi",
            first_message="kel YouTube uchun reja qilaylik",
        ))
        store = MemoStore(tmp_path)
        memo = store.load("project-thread-212432")
        assert memo is not None
        assert memo.type == MemoType.PROJECT
        assert memo.user_scope == "1545224574"
        assert memo.thread_scope == "212432"
        assert "YouTube kontenti rejasi" in memo.description
        assert "kel YouTube uchun reja qilaylik" in memo.body

    def test_scope_filters_correctly(self, tmp_path):
        titler = _make_titler(tmp_path)
        _run(titler._save_thread_context_memo(
            chat_id=100,
            thread_id=200,
            title="Test thread",
            first_message="hello",
        ))
        store = MemoStore(tmp_path)
        # Caller in the same scope sees the memo.
        in_scope = store.list_in_scope(user_id="100", thread_id="200")
        assert len(in_scope) == 1
        assert in_scope[0].name == "project-thread-200"
        # Caller in a different thread doesn't.
        wrong_thread = store.list_in_scope(user_id="100", thread_id="999")
        assert wrong_thread == []
        # Caller as different user doesn't either.
        wrong_user = store.list_in_scope(user_id="999", thread_id="200")
        assert wrong_user == []

    def test_long_title_truncated(self, tmp_path):
        titler = _make_titler(tmp_path)
        long_title = "A" * 250  # exceeds the 200-char description cap
        _run(titler._save_thread_context_memo(
            chat_id=1, thread_id=2, title=long_title, first_message="x",
        ))
        store = MemoStore(tmp_path)
        memo = store.load("project-thread-2")
        assert memo is not None
        assert len(memo.description) <= 200

    def test_long_first_message_excerpted(self, tmp_path):
        titler = _make_titler(tmp_path)
        long_msg = "very long first message " * 100
        _run(titler._save_thread_context_memo(
            chat_id=1, thread_id=2, title="Topic", first_message=long_msg,
        ))
        store = MemoStore(tmp_path)
        memo = store.load("project-thread-2")
        # Excerpt is capped around 300 chars in the body.
        assert memo is not None
        first_msg_section = memo.body.split("Birinchi xabar:", 1)[1].split("\n", 1)[0]
        assert len(first_msg_section) < 400

    def test_idempotent(self, tmp_path):
        """Saving twice on the same thread updates rather than duplicates."""
        titler = _make_titler(tmp_path)
        _run(titler._save_thread_context_memo(
            chat_id=1, thread_id=42, title="First title", first_message="msg",
        ))
        _run(titler._save_thread_context_memo(
            chat_id=1, thread_id=42, title="Refined title", first_message="msg",
        ))
        store = MemoStore(tmp_path)
        memos = store.list_all()
        names = [m.name for m in memos]
        assert names.count("project-thread-42") == 1
        # Latest title wins.
        assert "Refined title" in store.load("project-thread-42").description

    def test_failure_does_not_raise(self, tmp_path, monkeypatch):
        """A broken MemoStore must not propagate — titler's main job
        (renaming) already succeeded by the time this fires.
        """
        titler = _make_titler(tmp_path)
        # Force MemoStore.upsert to raise.
        from qanot.memos import MemoStore as RealStore
        original = RealStore.upsert
        try:
            def broken(self, *args, **kwargs):
                raise RuntimeError("disk full")
            monkeypatch.setattr(RealStore, "upsert", broken)
            # Should NOT raise — the function swallows errors and logs.
            _run(titler._save_thread_context_memo(
                chat_id=1, thread_id=2, title="x", first_message="y",
            ))
        finally:
            monkeypatch.setattr(RealStore, "upsert", original)


# ─── memo body content checks ───────────────────────────────────


class TestMemoBody:
    def test_body_carries_topic_directive(self, tmp_path):
        titler = _make_titler(tmp_path)
        _run(titler._save_thread_context_memo(
            chat_id=1, thread_id=2, title="IELTS strategiyasi",
            first_message="IELTS uchun strategiya tuzaylik",
        ))
        body = MemoStore(tmp_path).load("project-thread-2").body
        # Body must instruct the agent to respond in-topic when activity
        # resumes — this is the bug-class fix the memo exists to solve.
        assert "thread'da har doim shu mavzu" in body
        assert "generic javob emas" in body

    def test_body_carries_first_message(self, tmp_path):
        titler = _make_titler(tmp_path)
        first = "ertaga reklama kampaniya boshlaymiz"
        _run(titler._save_thread_context_memo(
            chat_id=1, thread_id=2, title="Reklama",
            first_message=first,
        ))
        body = MemoStore(tmp_path).load("project-thread-2").body
        assert first in body

    def test_empty_first_message_safe(self, tmp_path):
        # The titler skips empty messages upstream, but defensive coverage.
        titler = _make_titler(tmp_path)
        _run(titler._save_thread_context_memo(
            chat_id=1, thread_id=2, title="Empty test",
            first_message="",
        ))
        memo = MemoStore(tmp_path).load("project-thread-2")
        assert memo is not None
        assert "Birinchi xabar:" in memo.body
