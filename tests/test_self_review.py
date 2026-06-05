"""Tests for post-turn self-review (A1, qanot/self_review.py)."""

from __future__ import annotations

import asyncio
import types

import pytest

import qanot.self_review as sr


# ── helpers / fakes ───────────────────────────────────────────────────

def _block(text):
    return types.SimpleNamespace(type="text", text=text)


def _resp(text):
    return types.SimpleNamespace(content=[_block(text)])


def _fake_client(reply_text):
    class _Msgs:
        async def create(self, **kw):
            return _resp(reply_text)
    return types.SimpleNamespace(messages=_Msgs())


def _agent(client, workspace_dir):
    return types.SimpleNamespace(
        provider=types.SimpleNamespace(client=client),
        config=types.SimpleNamespace(workspace_dir=str(workspace_dir)),
        _review_turn_count={},
        _get_messages=lambda u: [],
    )


# ── parsing / text helpers ────────────────────────────────────────────

def test_parse_candidates():
    out = sr._parse_candidates('noise [{"observation":"o","lesson":"l"}] tail')
    assert out == [{"observation": "o", "lesson": "l"}]
    assert sr._parse_candidates("not json") == []
    assert sr._parse_candidates("[]") == []


def test_recent_text_skips_summaries_and_tools():
    msgs = [
        {"role": "user", "content": "[CONVERSATION SUMMARY — x]\n\nold"},
        {"role": "user", "content": "real question about bambuk pricing"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "x", "input": {}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "the answer"}]},
    ]
    out = sr._recent_text(msgs, 24)
    assert "real question about bambuk" in out
    assert "the answer" in out
    assert "CONVERSATION SUMMARY" not in out


# ── cadence ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cadence_fires_only_every_n(monkeypatch, tmp_path):
    calls = []

    async def fake_run(agent, uid, msgs):
        calls.append(uid)

    monkeypatch.setattr(sr, "_run_review", fake_run)
    agent = _agent(_fake_client("[]"), tmp_path)
    agent._get_messages = lambda u: [{"role": "user", "content": "x" * 100}]

    for _ in range(sr.REVIEW_EVERY_N_TURNS - 1):
        sr.schedule_self_review(agent, "u1")
    await asyncio.sleep(0)
    assert calls == []                      # not yet

    sr.schedule_self_review(agent, "u1")    # the Nth turn
    await asyncio.sleep(0)
    assert calls == ["u1"]
    assert agent._review_turn_count["u1"] == sr.REVIEW_EVERY_N_TURNS


def test_no_user_id_is_noop():
    agent = _agent(_fake_client("[]"), "/tmp")
    sr.schedule_self_review(agent, "")  # must not raise / not count
    assert agent._review_turn_count == {}


# ── the reflection pass + quality gate ────────────────────────────────

@pytest.mark.asyncio
async def test_run_review_captures_high_quality(monkeypatch, tmp_path):
    reply = '[{"observation":"pricelist used cost not sale price","lesson":"use order export for sale prices"}]'
    agent = _agent(_fake_client(reply), tmp_path)

    monkeypatch.setattr(
        "evals.judge_lesson.judge_lesson",
        lambda lesson, existing_lessons=None: types.SimpleNamespace(overall=82.0, summary="solid"),
    )
    await sr._run_review(agent, "u1", [
        {"role": "user", "content": "build the bambuk pricelist with sale prices please"},
        {"role": "assistant", "content": [{"type": "text", "text": "done, used order export"}]},
    ])

    from qanot.learnings import load_learnings
    lessons = load_learnings(str(tmp_path))
    keep = [l for l in lessons if "use order export" in l["lesson"]]
    assert keep and keep[0]["quality_score"] == 82.0
    assert "self-review" in keep[0]["tags"]


@pytest.mark.asyncio
async def test_run_review_drops_low_quality(monkeypatch, tmp_path):
    reply = '[{"observation":"trivial","lesson":"the sky is blue sometimes"}]'
    agent = _agent(_fake_client(reply), tmp_path)
    monkeypatch.setattr(
        "evals.judge_lesson.judge_lesson",
        lambda lesson, existing_lessons=None: types.SimpleNamespace(overall=35.0, summary="weak"),
    )
    await sr._run_review(agent, "u1", [
        {"role": "user", "content": "a fairly long message that exceeds the minimum length threshold for review"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok noted"}]},
    ])
    from qanot.learnings import load_learnings
    assert load_learnings(str(tmp_path)) == []  # nothing stored


@pytest.mark.asyncio
async def test_run_review_caps_at_max(monkeypatch, tmp_path):
    reply = (
        '[{"observation":"a","lesson":"lesson one about order export pricing"},'
        '{"observation":"b","lesson":"lesson two about category filtering logic"},'
        '{"observation":"c","lesson":"lesson three about customer scoping rules"}]'
    )
    agent = _agent(_fake_client(reply), tmp_path)
    monkeypatch.setattr(
        "evals.judge_lesson.judge_lesson",
        lambda lesson, existing_lessons=None: types.SimpleNamespace(overall=90.0, summary="ok"),
    )
    await sr._run_review(agent, "u1", [
        {"role": "user", "content": "a sufficiently long user message to pass the review length gate here"},
        {"role": "assistant", "content": [{"type": "text", "text": "answered"}]},
    ])
    from qanot.learnings import load_learnings
    assert len(load_learnings(str(tmp_path))) == sr.MAX_LESSONS  # capped at 2


@pytest.mark.asyncio
async def test_run_review_judge_unavailable_skips(monkeypatch, tmp_path):
    """If the judge errors, lessons are conservatively NOT stored (no unvetted)."""
    reply = '[{"observation":"o","lesson":"some plausible lesson about pricing data"}]'
    agent = _agent(_fake_client(reply), tmp_path)

    def _boom(lesson, existing_lessons=None):
        raise RuntimeError("judge down")

    monkeypatch.setattr("evals.judge_lesson.judge_lesson", _boom)
    await sr._run_review(agent, "u1", [
        {"role": "user", "content": "a long enough user message to clear the review length threshold ok"},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ])
    from qanot.learnings import load_learnings
    assert load_learnings(str(tmp_path)) == []
