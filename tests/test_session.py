"""Tests for session replay (restore_history)."""

from __future__ import annotations

import json
import pytest

from qanot.session import (
    SessionWriter,
    _entries_to_messages,
    _limit_history_turns,
    _sanitize_restored_messages,
    _strip_injection,
)


class TestStripInjection:
    def test_strips_memory_context(self):
        text = "hello\n\n---\n[MEMORY CONTEXT — relevant past]\n- some memory"
        assert _strip_injection(text) == "hello"

    def test_strips_compaction_recovery(self):
        text = "hello\n\n---\n\n[COMPACTION RECOVERY]\nsome recovery"
        assert _strip_injection(text) == "hello"

    def test_no_injection(self):
        assert _strip_injection("normal message") == "normal message"

    def test_empty(self):
        assert _strip_injection("") == ""


class TestLimitHistoryTurns:
    def test_limit_trims_old_turns(self):
        messages = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "resp1"},
            {"role": "user", "content": "msg2"},
            {"role": "assistant", "content": "resp2"},
            {"role": "user", "content": "msg3"},
            {"role": "assistant", "content": "resp3"},
        ]
        result = _limit_history_turns(messages, max_turns=2)
        # Keeps last 2 user turns + surrounding messages
        user_msgs = [m for m in result if m["role"] == "user"]
        assert len(user_msgs) == 2
        assert user_msgs[0]["content"] == "msg2"
        assert user_msgs[1]["content"] == "msg3"

    def test_limit_zero_returns_empty(self):
        messages = [{"role": "user", "content": "msg"}]
        assert _limit_history_turns(messages, max_turns=0) == []

    def test_under_limit_keeps_all(self):
        messages = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "resp1"},
        ]
        result = _limit_history_turns(messages, max_turns=10)
        assert len(result) == 2


class TestSanitizeRestoredMessages:
    def test_removes_empty_messages(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "world"},
        ]
        result = _sanitize_restored_messages(messages)
        assert all(m["content"] for m in result)

    def test_ensures_starts_with_user(self):
        messages = [
            {"role": "assistant", "content": "orphan"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        result = _sanitize_restored_messages(messages)
        assert result[0]["role"] == "user"

    def test_merges_consecutive_same_role(self):
        messages = [
            {"role": "user", "content": "part1"},
            {"role": "user", "content": "part2"},
            {"role": "assistant", "content": "response"},
        ]
        result = _sanitize_restored_messages(messages)
        assert len(result) == 2
        assert "part1\npart2" == result[0]["content"]

    def test_empty_input(self):
        assert _sanitize_restored_messages([]) == []


class TestEntriesToMessages:
    def test_basic_conversion(self):
        entries = [
            {"message": {"role": "user", "content": "hello"}},
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
        ]
        result = _entries_to_messages(entries)
        assert len(result) == 2
        assert result[0] == {"role": "user", "content": "hello"}
        assert result[1] == {"role": "assistant", "content": "hi"}

    def test_skips_tool_results(self):
        entries = [
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "1", "content": "result"},
            ]}},
        ]
        result = _entries_to_messages(entries)
        assert len(result) == 0  # tool results are skipped

    def test_strips_injection_artifacts(self):
        entries = [
            {"message": {"role": "user", "content": "query\n\n---\n[MEMORY CONTEXT — x]\n- mem"}},
        ]
        result = _entries_to_messages(entries)
        assert result[0]["content"] == "query"

    def test_skips_tool_use_blocks(self):
        entries = [
            {"message": {"role": "assistant", "content": [
                {"type": "text", "text": "thinking..."},
                {"type": "tool_use", "id": "1", "name": "read_file", "input": {}},
            ]}},
        ]
        result = _entries_to_messages(entries)
        assert len(result) == 1
        assert result[0]["content"] == "thinking..."


class TestSessionWriterRestore:
    def test_restore_empty(self, tmp_path):
        writer = SessionWriter(str(tmp_path))
        result = writer.restore_history("user123")
        assert result == []

    def test_restore_filters_by_user(self, tmp_path):
        writer = SessionWriter(str(tmp_path))

        # Write messages for two users
        writer.log_user_message("hello from A", user_id="userA")
        writer.log_assistant_message("hi A", user_id="userA")
        writer.log_user_message("hello from B", user_id="userB")
        writer.log_assistant_message("hi B", user_id="userB")

        restored_a = writer.restore_history("userA")
        assert len(restored_a) == 2
        assert restored_a[0]["content"] == "hello from A"
        assert restored_a[1]["content"] == "hi A"

        restored_b = writer.restore_history("userB")
        assert len(restored_b) == 2
        assert restored_b[0]["content"] == "hello from B"

    def test_restore_respects_max_turns(self, tmp_path):
        writer = SessionWriter(str(tmp_path))

        for i in range(10):
            writer.log_user_message(f"msg{i}", user_id="user1")
            writer.log_assistant_message(f"resp{i}", user_id="user1")

        result = writer.restore_history("user1", max_turns=3)
        # Should have 3 user turns = 6 messages (3 user + 3 assistant)
        user_msgs = [m for m in result if m["role"] == "user"]
        assert len(user_msgs) == 3
        assert user_msgs[0]["content"] == "msg7"  # Last 3 turns

    def test_restore_handles_corrupt_lines(self, tmp_path):
        writer = SessionWriter(str(tmp_path))
        writer.log_user_message("good msg", user_id="user1")
        writer.log_assistant_message("good resp", user_id="user1")

        # Inject a corrupt line
        with open(writer.session_path, "a") as f:
            f.write("not valid json\n")

        result = writer.restore_history("user1")
        assert len(result) == 2  # Corrupt line skipped
