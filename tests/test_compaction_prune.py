"""Tests for the cheap structural pre-prune (Hermes-borrow item #5).

Validates that prune_old_tool_results:
  - Drops bulk content from old tool_result blocks
  - Preserves the most recent N messages verbatim
  - Reports correct bytes_saved
  - Doesn't break tool_use/tool_result pairing (skeleton preserved)
  - Tolerates edge cases (empty messages, malformed shapes, short tool results)
"""

from __future__ import annotations

from qanot.compaction import (
    PRUNED_PLACEHOLDER,
    prune_old_tool_results,
)


def _msg(role: str, *blocks) -> dict:
    return {"role": role, "content": list(blocks)}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_use(tool_id: str, name: str) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def _tool_result(tool_id: str, content: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content}


# ── Core behavior ──────────────────────────────────────────────


def test_keep_recent_n_preserves_tail():
    """Most recent N messages must be untouched."""
    big_result = "x" * 10_000
    msgs = []
    for i in range(10):
        msgs.append(_msg("assistant", _tool_use(f"t{i}", "fake")))
        msgs.append(_msg("user", _tool_result(f"t{i}", big_result + str(i))))

    pruned, _ = prune_old_tool_results(msgs, keep_recent_n=4)
    # Last 4 messages keep verbose content
    for i in range(len(msgs) - 4, len(msgs)):
        assert pruned[i] == msgs[i], f"message {i} (tail) was modified"


def test_old_tool_results_replaced_with_placeholder():
    big_result = "x" * 10_000
    msgs = [
        _msg("assistant", _tool_use("t1", "fake")),
        _msg("user", _tool_result("t1", big_result)),
        _msg("assistant", _text("ok")),
        _msg("user", _text("hi")),
    ]
    pruned, bytes_saved = prune_old_tool_results(msgs, keep_recent_n=2)
    # First user message (with tool_result) should be pruned
    assert pruned[1]["content"][0]["content"] == PRUNED_PLACEHOLDER
    assert bytes_saved == 10_000 - len(PRUNED_PLACEHOLDER)


def test_short_tool_results_not_replaced():
    """Tiny results aren't worth pruning — would actually grow them."""
    msgs = [
        _msg("assistant", _tool_use("t1", "fake")),
        _msg("user", _tool_result("t1", "ok")),
        _msg("user", _text("more")),
        _msg("user", _text("more 2")),
        _msg("user", _text("more 3")),
    ]
    pruned, bytes_saved = prune_old_tool_results(msgs, keep_recent_n=2)
    assert pruned[1]["content"][0]["content"] == "ok"  # unchanged
    assert bytes_saved == 0


def test_text_blocks_preserved():
    """Only tool_result blocks are pruned. Text content is left alone."""
    long_text = "important user context\n" * 200
    msgs = [
        _msg("user", _text(long_text)),
        _msg("assistant", _text("response")),
        _msg("user", _text("follow-up")),
        _msg("user", _text("recent")),
    ]
    pruned, bytes_saved = prune_old_tool_results(msgs, keep_recent_n=1)
    # Long text in old message must NOT be touched
    assert pruned[0]["content"][0]["text"] == long_text
    assert bytes_saved == 0


def test_tool_use_blocks_preserved():
    """tool_use blocks (assistant's call) are preserved; only tool_result content is pruned."""
    big = "x" * 10_000
    msgs = [
        _msg("assistant", _tool_use("t1", "fake")),
        _msg("user", _tool_result("t1", big)),
        _msg("assistant", _text("done")),
        _msg("user", _text("recent")),
    ]
    pruned, _ = prune_old_tool_results(msgs, keep_recent_n=2)
    # tool_use is in an old assistant msg — should be untouched
    assert pruned[0]["content"][0]["type"] == "tool_use"
    assert pruned[0]["content"][0]["name"] == "fake"
    # tool_result for it gets the placeholder
    assert pruned[1]["content"][0]["content"] == PRUNED_PLACEHOLDER


def test_pairing_preserved_tool_use_and_result_both_kept():
    """Even after pruning, every tool_use still has a corresponding
    tool_result block (with placeholder content). API contract requires
    matching pairs."""
    big = "x" * 10_000
    msgs = [
        _msg("assistant", _tool_use("call_1", "tool_a"), _tool_use("call_2", "tool_b")),
        _msg("user", _tool_result("call_1", big), _tool_result("call_2", big)),
        _msg("assistant", _text("done")),
        _msg("user", _text("recent")),
    ]
    pruned, _ = prune_old_tool_results(msgs, keep_recent_n=2)
    # Both tool_use ids should still have matching tool_result blocks
    tool_use_ids = []
    tool_result_ids = []
    for msg in pruned:
        for block in msg.get("content", []):
            if isinstance(block, dict):
                if block.get("type") == "tool_use":
                    tool_use_ids.append(block["id"])
                elif block.get("type") == "tool_result":
                    tool_result_ids.append(block["tool_use_id"])
    assert set(tool_use_ids) == set(tool_result_ids) == {"call_1", "call_2"}


# ── Edge cases ─────────────────────────────────────────────────


def test_empty_messages():
    pruned, saved = prune_old_tool_results([], keep_recent_n=4)
    assert pruned == []
    assert saved == 0


def test_keep_recent_exceeds_total():
    """When keep_recent_n >= len(messages), nothing is pruned."""
    msgs = [
        _msg("user", _tool_result("t1", "x" * 10_000)),
    ]
    pruned, saved = prune_old_tool_results(msgs, keep_recent_n=4)
    assert pruned == msgs
    assert saved == 0


def test_string_content_messages_pass_through():
    """Some messages have string content (not block list). Skip them."""
    msgs = [
        {"role": "user", "content": "old message string"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "recent"},
    ]
    pruned, saved = prune_old_tool_results(msgs, keep_recent_n=1)
    assert pruned == msgs
    assert saved == 0


def test_assistant_messages_with_tool_results_unaffected():
    """Tool_result blocks normally appear in user messages, not assistant.
    If an assistant message has one (rare/unusual), don't prune it (we
    only prune user-role messages defensively)."""
    big = "x" * 10_000
    msgs = [
        # Unusual: tool_result in assistant role
        _msg("assistant", _tool_result("t1", big)),
        _msg("user", _text("recent")),
    ]
    pruned, saved = prune_old_tool_results(msgs, keep_recent_n=1)
    # Defensive: only prune user-role tool_result blocks
    assert pruned[0]["content"][0]["content"] == big
    assert saved == 0


def test_bytes_saved_accounts_for_placeholder():
    """bytes_saved = original_len - placeholder_len, summed across pruned blocks."""
    payload_a = "a" * 5_000
    payload_b = "b" * 8_000
    msgs = [
        _msg("user", _tool_result("t1", payload_a), _tool_result("t2", payload_b)),
        _msg("user", _text("tail")),
        _msg("user", _text("tail 2")),
    ]
    _, saved = prune_old_tool_results(msgs, keep_recent_n=2)
    expected = (5_000 - len(PRUNED_PLACEHOLDER)) + (8_000 - len(PRUNED_PLACEHOLDER))
    assert saved == expected


def test_list_content_in_tool_result_pruned():
    """Tool result content can be a list (multi-modal). Pruning still works."""
    big_list = ["x" * 5000, "y" * 5000]
    msgs = [
        _msg("user", {"type": "tool_result", "tool_use_id": "t1", "content": big_list}),
        _msg("user", _text("recent")),
        _msg("user", _text("recent 2")),
    ]
    pruned, saved = prune_old_tool_results(msgs, keep_recent_n=2)
    # Old block was pruned to placeholder string
    assert pruned[0]["content"][0]["content"] == PRUNED_PLACEHOLDER
    assert saved > 0


# ── Integration with metric event ──────────────────────────────


def test_compaction_event_includes_prune_metrics(tmp_path):
    """log_compaction_event accepts and persists tokens_before_prune + bytes_pruned."""
    from qanot.compaction_metrics import load_events, log_compaction_event
    log_compaction_event(
        str(tmp_path),
        tokens_before=2000, tokens_after=400,
        stage="full", duration_ms=500,
        tokens_before_prune=10000, bytes_pruned=15000,
    )
    events = load_events(str(tmp_path))
    assert len(events) == 1
    assert events[0]["tokens_before_prune"] == 10000
    assert events[0]["bytes_pruned"] == 15000


def test_compaction_event_omits_prune_fields_when_zero(tmp_path):
    """Don't bloat events with explicit zeros — fields only appear when meaningful."""
    from qanot.compaction_metrics import load_events, log_compaction_event
    log_compaction_event(str(tmp_path), tokens_before=2000, stage="full")
    events = load_events(str(tmp_path))
    assert "tokens_before_prune" not in events[0]
    assert "bytes_pruned" not in events[0]
