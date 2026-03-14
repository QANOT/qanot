"""Tests for ContextTracker and compaction."""

from __future__ import annotations

import pytest

from qanot.context import ContextTracker, truncate_tool_result
from qanot.providers.errors import (
    classify_error,
    is_context_overflow_error,
    ERROR_CONTEXT_OVERFLOW,
    ERROR_RATE_LIMIT,
)


class TestContextTracker:
    def test_initial_state(self):
        ct = ContextTracker(max_tokens=100_000)
        assert ct.total_tokens == 0
        assert ct.get_context_percent() == 0.0
        assert ct.buffer_active is False

    def test_add_usage(self):
        ct = ContextTracker(max_tokens=100_000)
        ct.add_usage(1000, 500)
        assert ct.last_prompt_tokens == 1000
        assert ct.total_output == 500
        assert ct.total_tokens == 1500  # last_prompt_tokens + total_output
        assert ct.api_calls == 1

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

    def test_threshold_activates_at_50(self, tmp_path):
        ct = ContextTracker(max_tokens=100_000, workspace_dir=str(tmp_path))
        # Simulate growing prompt tokens (as conversation builds up)
        ct.add_usage(40_000, 0)
        assert ct.check_threshold() is False
        assert ct.buffer_active is False

        # Prompt now exceeds 50% (new threshold)
        ct.add_usage(50_000, 0)
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
        assert status["turn_count"] == 0  # turn_count managed by agent.py, not add_usage
        assert status["api_calls"] == 1
        assert status["context_tokens"] == 50_000

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

    def test_compact_messages_with_summary(self):
        ct = ContextTracker(max_tokens=100_000)
        messages = [
            {"role": "user", "content": f"msg {i}"}
            for i in range(10)
        ]
        summary = "User discussed topics A, B, and C. Decision: go with B."
        compacted = ct.compact_messages(messages, summary_text=summary)
        assert len(compacted) == 7
        # Summary should contain the LLM text, not truncation marker
        assert "CONVERSATION SUMMARY" in compacted[2]["content"]
        assert "go with B" in compacted[2]["content"]
        assert "CONTEXT COMPACTION" not in compacted[2]["content"]

    def test_compact_messages_without_summary_fallback(self):
        ct = ContextTracker(max_tokens=100_000)
        messages = [
            {"role": "user", "content": f"msg {i}"}
            for i in range(10)
        ]
        # No summary = truncation marker
        compacted = ct.compact_messages(messages, summary_text=None)
        assert "CONTEXT COMPACTION" in compacted[2]["content"]

    def test_extract_compaction_text(self):
        messages = [
            {"role": "user", "content": "init 1"},
            {"role": "assistant", "content": "init 2"},
            {"role": "user", "content": "middle message 1"},
            {"role": "assistant", "content": "middle response 1"},
            {"role": "user", "content": "middle message 2"},
            {"role": "assistant", "content": "middle response 2"},
            {"role": "user", "content": "recent 1"},
            {"role": "assistant", "content": "recent 2"},
            {"role": "user", "content": "recent 3"},
            {"role": "assistant", "content": "recent 4"},
        ]
        text = ContextTracker.extract_compaction_text(messages)
        # Should contain middle messages but not head/tail
        assert "middle message 1" in text
        assert "middle response 2" in text
        assert "init 1" not in text
        assert "recent 4" not in text

    def test_extract_compaction_text_with_tool_blocks(self):
        messages = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "ok"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me check"},
                {"type": "tool_use", "name": "read_file", "id": "1", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "1", "content": "file contents here..."},
            ]},
            {"role": "user", "content": "recent 1"},
            {"role": "assistant", "content": "recent 2"},
            {"role": "user", "content": "recent 3"},
            {"role": "assistant", "content": "recent 4"},
        ]
        text = ContextTracker.extract_compaction_text(messages)
        assert "Let me check" in text
        assert "[tool: read_file]" in text

    def test_extract_compaction_text_too_few_messages(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        text = ContextTracker.extract_compaction_text(messages)
        assert text == ""


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


class TestContextOverflowDetection:
    def test_anthropic_overflow(self):
        assert is_context_overflow_error("context_window_exceeded")

    def test_openai_overflow(self):
        assert is_context_overflow_error("maximum context length exceeded")

    def test_generic_overflow(self):
        assert is_context_overflow_error("prompt is too long for this model")

    def test_too_many_tokens(self):
        assert is_context_overflow_error("too many tokens in the request")

    def test_request_too_large(self):
        assert is_context_overflow_error("request_too_large")

    def test_not_overflow(self):
        assert not is_context_overflow_error("rate limit exceeded")
        assert not is_context_overflow_error("unauthorized")
        assert not is_context_overflow_error("internal server error")

    def test_classify_overflow_error(self):
        err = Exception("This request exceeds the maximum context length")
        assert classify_error(err) == ERROR_CONTEXT_OVERFLOW

    def test_classify_rate_limit_not_overflow(self):
        err = Exception("rate limit exceeded")
        assert classify_error(err) == ERROR_RATE_LIMIT
