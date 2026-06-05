"""Context-polish: last-user-message anchor on compaction."""

from __future__ import annotations

import json

import pytest

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


# ── #2: persistent compaction summary ─────────────────────────────────

def test_extract_summary_body():
    from qanot.flush import _extract_summary_body
    block = ("[CONVERSATION SUMMARY — 3 messages compacted]\n\n"
             "OLD FACTS: bambuk=falcon, price source=order$export\n\n"
             "[End of summary. Recent conversation continues below.]")
    assert _extract_summary_body(block) == "OLD FACTS: bambuk=falcon, price source=order$export"


@pytest.mark.asyncio
async def test_compaction_reuses_prior_summary(tmp_path):
    """A second compaction must UPDATE the prior [CONVERSATION SUMMARY],
    not re-summarize it from scratch — so the prior summary text reaches the
    summarizer as base context."""
    from qanot.config import Config
    from qanot.context import ContextTracker
    from qanot.flush import summarize_for_compaction
    from qanot.providers.base import ProviderResponse

    captured: list[str] = []

    class _FakeProvider:
        model = "fake"

        async def chat(self, messages, tools=None, system=None):
            captured.append(messages[-1]["content"])
            return ProviderResponse(
                content="Updated summary integrating the prior facts and new markup rule.")

    cfg = Config(
        workspace_dir=str(tmp_path / "ws"),
        sessions_dir=str(tmp_path / "s"),
        cron_dir=str(tmp_path / "c"),
        compaction_mode="safeguard",
        max_context_tokens=200_000,
    )
    import os
    os.makedirs(cfg.workspace_dir, exist_ok=True)

    prior = ("[CONVERSATION SUMMARY — 3 messages compacted]\n\n"
             "PRIOR: user is Falcon distributor; sale prices via order$export\n\n"
             "[End of summary. Recent conversation continues below.]")
    msgs = [
        {"role": "user", "content": "init"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": prior},                  # the prior summary
        {"role": "user", "content": "new: add 30% markup default"},
        {"role": "assistant", "content": "noted the markup"},
        {"role": "user", "content": "and round to 1000"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "recent ask"},
        {"role": "assistant", "content": "working"},
    ]
    out = await summarize_for_compaction(
        msgs, _FakeProvider(), cfg, ContextTracker(max_tokens=200_000),
    )
    assert out  # a summary was produced
    # The prior summary's content must have reached the summarizer as base.
    joined = "\n".join(captured)
    assert "PRIOR: user is Falcon distributor" in joined
