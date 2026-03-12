"""Tests for multi-stage compaction module."""

from __future__ import annotations

import pytest

from qanot.compaction import (
    estimate_tokens,
    estimate_message_tokens,
    estimate_messages_tokens,
    split_messages_by_token_share,
    chunk_messages_by_max_tokens,
    compute_adaptive_chunk_ratio,
    is_oversized_for_summary,
    strip_tool_result_details,
    messages_to_text,
    prune_history_for_context,
    _repair_orphaned_tool_results,
    BASE_CHUNK_RATIO,
    MIN_CHUNK_RATIO,
)


# ── Helpers ──

def _make_msg(role: str, text: str) -> dict:
    return {"role": role, "content": text}


def _make_conversation(turns: int) -> list[dict]:
    """Generate a conversation with N user/assistant turn pairs."""
    msgs = []
    for i in range(turns):
        msgs.append(_make_msg("user", f"User message {i}: " + "x" * 100))
        msgs.append(_make_msg("assistant", f"Assistant response {i}: " + "y" * 100))
    return msgs


# ── Token estimation ──

class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 1  # min 1

    def test_short_text(self):
        assert estimate_tokens("hello") == 1

    def test_long_text(self):
        tokens = estimate_tokens("a" * 400)
        assert tokens == 100  # 400 / 4

    def test_message_tokens(self):
        msg = _make_msg("user", "a" * 40)
        tokens = estimate_message_tokens(msg)
        assert tokens == 14  # 40/4 + 4 overhead

    def test_messages_total(self):
        msgs = [_make_msg("user", "a" * 40), _make_msg("assistant", "b" * 40)]
        total = estimate_messages_tokens(msgs)
        assert total == 28  # (10 + 4) * 2


# ── Splitting ──

class TestSplitMessages:
    def test_empty(self):
        assert split_messages_by_token_share([], 2) == []

    def test_single_part(self):
        msgs = _make_conversation(3)
        result = split_messages_by_token_share(msgs, 1)
        assert len(result) == 1
        assert result[0] == msgs

    def test_two_parts(self):
        msgs = _make_conversation(10)
        result = split_messages_by_token_share(msgs, 2)
        assert len(result) == 2
        # All messages accounted for
        total = sum(len(chunk) for chunk in result)
        assert total == len(msgs)

    def test_three_parts(self):
        msgs = _make_conversation(12)
        result = split_messages_by_token_share(msgs, 3)
        assert len(result) <= 3
        total = sum(len(chunk) for chunk in result)
        assert total == len(msgs)

    def test_more_parts_than_messages(self):
        msgs = _make_conversation(2)
        result = split_messages_by_token_share(msgs, 10)
        total = sum(len(chunk) for chunk in result)
        assert total == len(msgs)


class TestChunkByMaxTokens:
    def test_empty(self):
        assert chunk_messages_by_max_tokens([], 1000) == []

    def test_fits_in_one(self):
        msgs = [_make_msg("user", "short")]
        result = chunk_messages_by_max_tokens(msgs, 1000)
        assert len(result) == 1

    def test_splits_on_limit(self):
        msgs = _make_conversation(20)  # Many messages
        result = chunk_messages_by_max_tokens(msgs, 200)  # Small limit
        assert len(result) > 1
        total = sum(len(chunk) for chunk in result)
        assert total == len(msgs)


# ── Adaptive chunk ratio ──

class TestAdaptiveChunkRatio:
    def test_small_messages(self):
        msgs = _make_conversation(10)
        ratio = compute_adaptive_chunk_ratio(msgs, 200_000)
        assert ratio == BASE_CHUNK_RATIO  # No reduction needed

    def test_large_messages(self):
        # Messages that are >10% of context
        msgs = [_make_msg("user", "x" * 100_000)]
        ratio = compute_adaptive_chunk_ratio(msgs, 200_000)
        assert ratio < BASE_CHUNK_RATIO
        assert ratio >= MIN_CHUNK_RATIO

    def test_empty(self):
        assert compute_adaptive_chunk_ratio([], 200_000) == BASE_CHUNK_RATIO


# ── Oversized detection ──

class TestOversized:
    def test_normal_message(self):
        msg = _make_msg("user", "short message")
        assert not is_oversized_for_summary(msg, 200_000)

    def test_huge_message(self):
        msg = _make_msg("user", "x" * 500_000)
        assert is_oversized_for_summary(msg, 200_000)


# ── Strip tool results ──

class TestStripToolResults:
    def test_no_tool_results(self):
        msgs = [_make_msg("user", "hello")]
        assert strip_tool_result_details(msgs) == msgs

    def test_truncates_large_results(self):
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "1", "content": "x" * 1000},
        ]}]
        result = strip_tool_result_details(msgs)
        content = result[0]["content"][0]["content"]
        assert len(content) < 400  # Truncated


# ── Messages to text ──

class TestMessagesToText:
    def test_basic(self):
        msgs = [_make_msg("user", "hello"), _make_msg("assistant", "hi")]
        text = messages_to_text(msgs)
        assert "**user**: hello" in text
        assert "**assistant**: hi" in text

    def test_tool_use(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "text", "text": "thinking"},
            {"type": "tool_use", "name": "read_file", "input": {}},
        ]}]
        text = messages_to_text(msgs)
        assert "[tool: read_file]" in text


# ── History pruning ──

class TestPruneHistory:
    def test_under_budget(self):
        msgs = _make_conversation(5)
        pruned, dropped = prune_history_for_context(msgs, 200_000)
        assert len(pruned) == len(msgs)
        assert dropped == 0

    def test_over_budget(self):
        msgs = _make_conversation(100)
        pruned, dropped = prune_history_for_context(msgs, 1000, max_history_share=0.5)
        assert len(pruned) < len(msgs)
        assert dropped > 0


# ── Orphaned tool result repair ──

class TestRepairOrphans:
    def test_no_orphans(self):
        msgs = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "test", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ]},
        ]
        result = _repair_orphaned_tool_results(msgs)
        assert len(result) == 2

    def test_removes_orphans(self):
        msgs = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "missing_id", "content": "orphan"},
            ]},
        ]
        result = _repair_orphaned_tool_results(msgs)
        assert len(result) == 0  # Orphan removed, message empty

    def test_keeps_text_with_orphan(self):
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_result", "tool_use_id": "missing", "content": "orphan"},
            ]},
        ]
        result = _repair_orphaned_tool_results(msgs)
        assert len(result) == 1
        assert len(result[0]["content"]) == 1  # Only text kept
