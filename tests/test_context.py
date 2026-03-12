"""Tests for ContextTracker."""

from __future__ import annotations

import pytest

from qanot.context import ContextTracker, truncate_tool_result


class TestContextTracker:
    def test_initial_state(self):
        ct = ContextTracker(max_tokens=100_000)
        assert ct.total_tokens == 0
        assert ct.get_context_percent() == 0.0
        assert ct.buffer_active is False

    def test_add_usage(self):
        ct = ContextTracker(max_tokens=100_000)
        ct.add_usage(1000, 500)
        assert ct.total_input == 1000
        assert ct.total_output == 500
        assert ct.total_tokens == 1500

    def test_context_percent_uses_last_prompt_tokens(self):
        ct = ContextTracker(max_tokens=100_000)
        ct.add_usage(60_000, 0)
        # last_prompt_tokens = 60_000 (the last call's input)
        assert ct.get_context_percent() == 60.0

    def test_context_percent_tracks_real_prompt_size(self):
        """Each API call reports the ACTUAL prompt size, not cumulative."""
        ct = ContextTracker(max_tokens=100_000)
        ct.add_usage(10_000, 500)  # Turn 1: 10K prompt
        assert ct.last_prompt_tokens == 10_000
        ct.add_usage(15_000, 600)  # Turn 2: 15K prompt (includes history)
        assert ct.last_prompt_tokens == 15_000
        assert ct.get_context_percent() == 15.0  # Based on last prompt

    def test_threshold_activates_at_60(self, tmp_path):
        ct = ContextTracker(max_tokens=100_000, workspace_dir=str(tmp_path))
        # Simulate growing prompt tokens (as conversation builds up)
        ct.add_usage(50_000, 0)
        assert ct.check_threshold() is False
        assert ct.buffer_active is False

        # Prompt now exceeds 60%
        ct.add_usage(60_000, 0)
        assert ct.check_threshold() is True
        assert ct.buffer_active is True

    def test_threshold_fires_once(self, tmp_path):
        ct = ContextTracker(max_tokens=100_000, workspace_dir=str(tmp_path))
        ct.add_usage(70_000, 0)
        assert ct.check_threshold() is True
        # Second call should return False (already active)
        assert ct.check_threshold() is False

    def test_working_buffer_file_created(self, tmp_path):
        ct = ContextTracker(max_tokens=100_000, workspace_dir=str(tmp_path))
        ct.add_usage(60_000, 0)
        ct.check_threshold()
        assert (tmp_path / "memory" / "working-buffer.md").exists()

    def test_append_to_buffer(self, tmp_path):
        ct = ContextTracker(max_tokens=100_000, workspace_dir=str(tmp_path))
        ct.add_usage(60_000, 0)
        ct.check_threshold()
        ct.append_to_buffer("User asked X", "Agent replied Y")

        content = (tmp_path / "memory" / "working-buffer.md").read_text()
        assert "User asked X" in content
        assert "Agent replied Y" in content

    def test_append_inactive_noop(self, tmp_path):
        ct = ContextTracker(max_tokens=100_000, workspace_dir=str(tmp_path))
        ct.append_to_buffer("ignored", "ignored")
        assert not (tmp_path / "memory" / "working-buffer.md").exists()

    def test_detect_compaction(self):
        ct = ContextTracker()
        assert ct.detect_compaction([]) is False
        assert ct.detect_compaction([{"content": "hello"}]) is False
        assert ct.detect_compaction([{"content": "<summary>old context</summary>"}]) is True
        assert ct.detect_compaction([{"content": "where were we?"}]) is True
        assert ct.detect_compaction([{"content": "CONTEXT COMPACTION: 5 messages removed"}]) is True

    def test_recover_from_compaction(self, tmp_path):
        ct = ContextTracker(workspace_dir=str(tmp_path))

        # Create session state
        (tmp_path / "SESSION-STATE.md").write_text("Important state info")

        recovery = ct.recover_from_compaction()
        assert "Important state info" in recovery

    def test_session_status(self):
        ct = ContextTracker(max_tokens=200_000)
        ct.add_usage(50_000, 10_000)
        status = ct.session_status()
        assert status["total_tokens"] == 60_000
        assert status["context_percent"] == 25.0
        assert status["buffer_active"] is False
        assert status["turn_count"] == 1
        assert status["last_prompt_tokens"] == 50_000

    def test_zero_max_tokens(self):
        ct = ContextTracker(max_tokens=0)
        assert ct.get_context_percent() == 0.0

    def test_needs_compaction(self):
        ct = ContextTracker(max_tokens=100_000)
        ct.add_usage(10_000, 1_000)
        assert ct.needs_compaction() is False

        # Simulate high context usage
        ct.add_usage(65_000, 5_000)
        assert ct.needs_compaction() is True

    def test_compact_messages(self):
        ct = ContextTracker(max_tokens=100_000)
        messages = [
            {"role": "user", "content": f"msg {i}"}
            for i in range(10)
        ]
        compacted = ct.compact_messages(messages)
        # Should keep first 2 + summary + last 4 = 7
        assert len(compacted) == 7
        # First message preserved
        assert compacted[0]["content"] == "msg 0"
        # Summary marker in middle
        assert "CONTEXT COMPACTION" in compacted[2]["content"]
        # Last messages preserved
        assert compacted[-1]["content"] == "msg 9"

    def test_compact_messages_too_few(self):
        ct = ContextTracker(max_tokens=100_000)
        messages = [{"role": "user", "content": "hello"}] * 5
        compacted = ct.compact_messages(messages)
        assert len(compacted) == 5  # Not compacted


class TestTruncateToolResult:
    def test_short_result_unchanged(self):
        result = "short result"
        assert truncate_tool_result(result) == result

    def test_long_result_truncated(self):
        result = "x" * 20_000
        truncated = truncate_tool_result(result, max_chars=1_000)
        assert len(truncated) < 20_000
        assert "truncated" in truncated

    def test_preserves_head_and_tail(self):
        result = "HEAD" * 100 + "MIDDLE" * 1000 + "TAIL" * 100
        truncated = truncate_tool_result(result, max_chars=1_000)
        assert truncated.startswith("HEAD")
        assert "TAIL" in truncated  # tail portion preserved
