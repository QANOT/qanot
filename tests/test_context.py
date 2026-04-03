"""Tests for ContextTracker and compaction."""

from __future__ import annotations

import pytest

from qanot.context import (
    ContextTracker,
    PERSIST_PREVIEW_CHARS,
    _MAX_TOOL_RESULT_FILES,
    persist_tool_result,
    truncate_tool_result,
)
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

    def test_persists_to_disk_when_workspace_provided(self, tmp_path):
        result = "A" * 10_000
        output = truncate_tool_result(
            result, tool_name="read_file", workspace_dir=str(tmp_path),
        )
        # Should contain preview + file path note
        assert output.startswith("A" * PERSIST_PREVIEW_CHARS)
        assert "[Full result (10000 chars) saved to:" in output
        assert "read_file_" in output
        assert "[Use read_file to access the full result if needed]" in output
        # Verify the file was actually created
        results_dir = tmp_path / ".tool-results"
        assert results_dir.exists()
        files = list(results_dir.iterdir())
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == result

    def test_falls_back_to_truncation_without_workspace(self):
        result = "B" * 10_000
        output = truncate_tool_result(result, max_chars=1_000)
        # No file path note — just truncated
        assert "[Full result" not in output
        assert "truncated" in output

    def test_short_result_unchanged_with_workspace(self, tmp_path):
        result = "short"
        output = truncate_tool_result(
            result, tool_name="test", workspace_dir=str(tmp_path),
        )
        assert output == "short"
        # No directory created for short results
        assert not (tmp_path / ".tool-results").exists()


class TestPersistToolResult:
    def test_creates_directory_and_file(self, tmp_path):
        result = "X" * 5_000
        output = persist_tool_result(result, "my_tool", str(tmp_path))
        results_dir = tmp_path / ".tool-results"
        assert results_dir.exists()
        files = list(results_dir.iterdir())
        assert len(files) == 1
        assert files[0].name.startswith("my_tool_")
        assert files[0].name.endswith(".txt")
        assert files[0].read_text(encoding="utf-8") == result
        assert output.startswith("X" * PERSIST_PREVIEW_CHARS)

    def test_cleanup_old_files(self, tmp_path):
        results_dir = tmp_path / ".tool-results"
        results_dir.mkdir()
        # Create more than the limit
        for i in range(_MAX_TOOL_RESULT_FILES + 10):
            (results_dir / f"old_{i:04d}.txt").write_text("data")
        assert len(list(results_dir.iterdir())) == _MAX_TOOL_RESULT_FILES + 10
        # Persist one more — should trigger cleanup
        persist_tool_result("Y" * 5_000, "new_tool", str(tmp_path))
        remaining = list(results_dir.iterdir())
        assert len(remaining) <= _MAX_TOOL_RESULT_FILES

    def test_empty_tool_name_defaults_to_unknown(self, tmp_path):
        persist_tool_result("data", "", str(tmp_path))
        files = list((tmp_path / ".tool-results").iterdir())
        assert len(files) == 1
        assert files[0].name.startswith("unknown_")


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


class TestSnip:
    """Tests for the snip compaction tier (strip old tool results, no LLM)."""

    def test_needs_snip_below_threshold(self):
        ct = ContextTracker(max_tokens=100_000)
        ct.last_prompt_tokens = 30_000  # 30% — below 40%
        assert ct.needs_snip() is False

    def test_needs_snip_above_threshold(self):
        ct = ContextTracker(max_tokens=100_000)
        ct.last_prompt_tokens = 45_000  # 45% — above 40%
        assert ct.needs_snip() is True

    def test_needs_snip_zero_max(self):
        ct = ContextTracker(max_tokens=0)
        assert ct.needs_snip() is False

    def test_snip_skips_recent_messages(self):
        """Last SNIP_KEEP_RECENT messages should never be snipped."""
        ct = ContextTracker(max_tokens=100_000)
        long_result = "x" * 1000
        messages = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": long_result},
            ]},
        ]
        # Only 1 message — fewer than SNIP_KEEP_RECENT (6)
        result, freed = ct.snip_messages(messages)
        assert freed == 0
        assert result is messages  # returned as-is

    def test_snip_strips_old_tool_results(self):
        """Old verbose tool results should be replaced with a short note."""
        ct = ContextTracker(max_tokens=100_000)
        long_result = "x" * 2000
        # Build 8 messages: first 2 have tool results, last 6 are recent
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "check this"},
                {"type": "tool_result", "tool_use_id": "t1", "content": long_result},
            ]},
            {"role": "assistant", "content": "ok"},
        ] + [
            {"role": "user", "content": "msg"},
            {"role": "assistant", "content": "reply"},
        ] * 3  # 6 more messages → total 8

        result, freed = ct.snip_messages(messages)
        assert freed > 0
        # The first message's tool_result should be snipped
        snipped_block = result[0]["content"][1]
        assert "snipped" in snipped_block["content"]
        assert "2000 chars" in snipped_block["content"]
        # Text block should be preserved
        assert result[0]["content"][0]["text"] == "check this"

    def test_snip_preserves_short_tool_results(self):
        """Tool results <= 500 chars should not be snipped."""
        ct = ContextTracker(max_tokens=100_000)
        short_result = "x" * 200
        messages = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": short_result},
            ]},
        ] + [{"role": "user", "content": "msg"}] * 6  # pad to exceed SNIP_KEEP_RECENT

        result, freed = ct.snip_messages(messages)
        assert freed == 0
        assert result[0]["content"][0]["content"] == short_result

    def test_snip_does_not_mutate_original(self):
        """Original messages list and dicts must not be modified."""
        ct = ContextTracker(max_tokens=100_000)
        long_result = "y" * 800
        original_msg = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": long_result},
        ]}
        messages = [original_msg] + [{"role": "user", "content": "msg"}] * 6

        result, freed = ct.snip_messages(messages)
        assert freed > 0
        # Original must be untouched
        assert original_msg["content"][0]["content"] == long_result

    def test_snip_handles_nested_content_blocks(self):
        """Tool results with list content (nested blocks) should be snipped."""
        ct = ContextTracker(max_tokens=100_000)
        nested_content = [{"type": "text", "text": "a" * 1000}]
        messages = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": nested_content},
            ]},
        ] + [{"role": "user", "content": "msg"}] * 6

        result, freed = ct.snip_messages(messages)
        assert freed > 0
        assert "snipped" in result[0]["content"][0]["content"]

    def test_snip_skips_assistant_messages(self):
        """Only user messages with tool_result blocks should be snipped."""
        ct = ContextTracker(max_tokens=100_000)
        messages = [
            {"role": "assistant", "content": [
                {"type": "text", "text": "x" * 2000},
            ]},
        ] + [{"role": "user", "content": "msg"}] * 6

        result, freed = ct.snip_messages(messages)
        assert freed == 0
