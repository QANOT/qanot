"""Tests for qanot.memos.prompt — <system-reminder> rendering."""

from __future__ import annotations

from pathlib import Path

from qanot.memos import MemoSpec, MemoType
from qanot.memos.prompt import (
    estimate_token_cost,
    render_system_reminder,
)
from qanot.memos.router import RouteResult, Selection


def _spec(
    name: str, description: str, body: str = "body",
    memo_type: MemoType = MemoType.FEEDBACK,
    why: str = "", how_to_apply: str = "",
) -> MemoSpec:
    return MemoSpec(
        name=name, description=description, type=memo_type,
        body=body, path=Path(f"/tmp/{name}.md"),
        why=why, how_to_apply=how_to_apply,
    )


# ─── empty result ───────────────────────────────────────────────


def test_empty_result_returns_empty_string():
    result = RouteResult(selections=[], candidates=0,
                        above_threshold=0, dropped_for_budget=0)
    assert render_system_reminder(result) == ""
    assert estimate_token_cost(result) == 0


# ─── basic rendering ────────────────────────────────────────────


def test_single_memo_block():
    spec = _spec(
        "feedback-title-format",
        "Daily note title must use D-month YYYY format",
        body="Title: 12-may, 2026",
        why="user said so",
        how_to_apply="when writing Notion titles",
    )
    result = RouteResult(
        selections=[Selection(memo=spec, score=0.9)],
        candidates=1, above_threshold=1, dropped_for_budget=0,
    )
    out = render_system_reminder(result)
    assert "<system-reminder>" in out
    assert "</system-reminder>" in out
    assert "Hard rule — feedback-title-format" in out
    assert "Daily note title must use D-month YYYY format" in out
    assert "Title: 12-may, 2026" in out
    assert "**Why:** user said so" in out
    assert "**How to apply:** when writing Notion titles" in out


def test_header_label_varies_by_type():
    user_memo = _spec("u", "user fact", memo_type=MemoType.USER)
    fb_memo = _spec("f", "feedback fact", memo_type=MemoType.FEEDBACK)
    proj_memo = _spec("p", "project fact", memo_type=MemoType.PROJECT)
    ref_memo = _spec("r", "ref fact", memo_type=MemoType.REFERENCE)
    result = RouteResult(
        selections=[
            Selection(memo=user_memo, score=0.9),
            Selection(memo=fb_memo, score=0.8),
            Selection(memo=proj_memo, score=0.7),
            Selection(memo=ref_memo, score=0.6),
        ],
        candidates=4, above_threshold=4, dropped_for_budget=0,
    )
    out = render_system_reminder(result)
    assert "User context — u" in out
    assert "Hard rule — f" in out
    assert "Project context — p" in out
    assert "Reference pointer — r" in out


def test_why_omitted_when_empty():
    spec = _spec("x", "desc", body="just body", why="", how_to_apply="")
    result = RouteResult(
        selections=[Selection(memo=spec, score=0.9)],
        candidates=1, above_threshold=1, dropped_for_budget=0,
    )
    out = render_system_reminder(result)
    assert "**Why:**" not in out
    assert "**How to apply:**" not in out


def test_how_already_in_body_not_duplicated():
    # If body already has the **How to apply:** line verbatim, we don't
    # repeat it under the auto-extracted Why/How block.
    body = (
        "Do X. Do Y.\n\n"
        "**How to apply:** when writing the daily title"
    )
    spec = _spec(
        "x", "desc",
        body=body,
        how_to_apply="when writing the daily title",
    )
    result = RouteResult(
        selections=[Selection(memo=spec, score=0.9)],
        candidates=1, above_threshold=1, dropped_for_budget=0,
    )
    out = render_system_reminder(result)
    # Exactly one occurrence of the **How to apply:** line.
    assert out.count("**How to apply:** when writing the daily title") == 1


# ─── multiple memos ─────────────────────────────────────────────


def test_multiple_memos_in_single_block():
    memos = [
        _spec("a", "first memo desc"),
        _spec("b", "second memo desc"),
        _spec("c", "third memo desc"),
    ]
    result = RouteResult(
        selections=[Selection(memo=m, score=0.9 - i * 0.1)
                    for i, m in enumerate(memos)],
        candidates=3, above_threshold=3, dropped_for_budget=0,
    )
    out = render_system_reminder(result)
    # Single block — only one open/close tag pair.
    assert out.count("<system-reminder>") == 1
    assert out.count("</system-reminder>") == 1
    # All three memos present.
    assert "Hard rule — a" in out
    assert "Hard rule — b" in out
    assert "Hard rule — c" in out


# ─── token estimate ─────────────────────────────────────────────


def test_token_estimate_scales_with_content():
    short = _spec("s", "short", body="short body")
    long = _spec("l", "x" * 100, body="x" * 1000)
    short_r = RouteResult(
        selections=[Selection(memo=short, score=0.9)],
        candidates=1, above_threshold=1, dropped_for_budget=0,
    )
    long_r = RouteResult(
        selections=[Selection(memo=long, score=0.9)],
        candidates=1, above_threshold=1, dropped_for_budget=0,
    )
    assert estimate_token_cost(short_r) < estimate_token_cost(long_r)
