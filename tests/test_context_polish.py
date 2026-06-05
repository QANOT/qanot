"""Context-polish: last-user-message anchor on compaction."""

from __future__ import annotations

import json

from qanot.context import ContextTracker, _last_user_request_index


def _tool_use(i):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": str(i), "name": "x", "input": {}}]}


def _tool_result(i):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": "r"}]}


def test_last_user_request_index_skips_tool_results():
    msgs = [
        {"role": "user", "content": "real ask"},          # 0
        _tool_use(1),                                      # 1
        _tool_result(1),                                   # 2 (user, but list content)
    ]
    assert _last_user_request_index(msgs) == 0
    assert _last_user_request_index([_tool_result(1)]) is None


def test_compaction_anchors_active_task_pushed_out_of_tail():
    msgs = [
        {"role": "user", "content": "init"},               # 0 head
        {"role": "assistant", "content": "hi"},            # 1 head
        {"role": "user", "content": "old chatter 1"},      # 2 middle
        {"role": "assistant", "content": "reply 1"},       # 3 middle
        {"role": "user", "content": "old chatter 2"},      # 4 middle
        {"role": "user", "content": "ACTIVE TASK: bambuk pricelist"},  # 5 the real ask
        _tool_use(6), _tool_result(6),                     # 6,7
        _tool_use(8), _tool_result(8),                     # 8,9
        _tool_use(10), _tool_result(10),                   # 10,11 — last 4 are tool turns
    ]
    ct = ContextTracker(max_tokens=200_000)
    out = ct.compact_messages(msgs, summary_text="SUMMARY", conv_key="u1")
    blob = json.dumps(out)
    # The active task must survive verbatim, NOT be folded into the summary.
    assert "ACTIVE TASK: bambuk pricelist" in blob
    # And it must be a real preserved message, not just text inside the summary.
    assert any(m.get("content") == "ACTIVE TASK: bambuk pricelist" for m in out)


def test_compaction_unchanged_when_request_already_recent():
    # Active task within the last keep_recent → no anchor extension needed.
    msgs = [
        {"role": "user", "content": "init"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "m2"},
        {"role": "user", "content": "m3"},
        {"role": "assistant", "content": "m4"},
        {"role": "user", "content": "recent ask"},
        {"role": "assistant", "content": "working"},
    ]
    ct = ContextTracker(max_tokens=200_000)
    out = ct.compact_messages(msgs, summary_text="S", conv_key="u1")
    blob = json.dumps(out)
    assert "recent ask" in blob
