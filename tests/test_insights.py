"""Tests for the usage insights engine (qanot/insights.py)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from qanot.insights import generate_insights, format_insights, _current_streak


def _ts(d: date, hour: int = 10) -> str:
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).isoformat()


def _write(sessions, name, entries):
    with open(sessions / f"{name}.jsonl", "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _assistant(d, *, tools=None, model="claude-opus-4-8", cost=0.01, hour=10):
    content = [{"type": "text", "text": "ok"}]
    for t in tools or []:
        content.append({"type": "tool_use", "name": t, "input": {}})
    return {
        "type": "message", "timestamp": _ts(d, hour), "model": model,
        "message": {"role": "assistant", "content": content},
        "usage": {"input": 100, "output": 20, "cacheRead": 50, "cacheWrite": 0,
                  "cost": {"total": cost}},
        "user_id": "42",
    }


def test_aggregates_tools_cost_models(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    today = date.today()
    _write(sessions, today.isoformat(), [
        _assistant(today, tools=["web_search", "web_search", "read_file"], cost=0.02),
        _assistant(today, tools=["web_search"], model="claude-sonnet-4-6", cost=0.005),
    ])

    data = generate_insights(str(sessions), days=30)
    assert not data["empty"]
    ov = data["overview"]
    assert ov["turns"] == 2
    assert abs(ov["cost_usd"] - 0.025) < 1e-6
    # top tool is web_search (3×)
    tools = dict(data["top_tools"])
    assert tools["web_search"] == 3 and tools["read_file"] == 1
    # model breakdown sorted by cost desc → opus first
    assert data["by_model"][0]["model"] == "claude-opus-4-8"
    assert data["by_model"][0]["turns"] == 1


def test_streak_counts_consecutive_days():
    today = date.today()
    dates = {(today - timedelta(days=i)).isoformat() for i in range(3)}  # today,-1,-2
    assert _current_streak(dates) == 3
    # gap breaks the streak
    gapped = {today.isoformat(), (today - timedelta(days=2)).isoformat()}
    assert _current_streak(gapped) == 1
    assert _current_streak(set()) == 0


def test_old_entries_excluded_by_window(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    old = date.today() - timedelta(days=40)
    _write(sessions, old.isoformat(), [_assistant(old, tools=["web_search"])])
    data = generate_insights(str(sessions), days=30)
    # 40-day-old file is older than the 30-day window → excluded
    assert data["empty"] or data["overview"]["turns"] == 0


def test_empty_and_missing_dir(tmp_path):
    assert generate_insights(str(tmp_path / "nope"), days=30)["empty"]
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    assert generate_insights(str(sessions), days=30)["empty"]


def test_format_insights_renders(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    today = date.today()
    _write(sessions, today.isoformat(), [_assistant(today, tools=["smartup_search_products"])])
    out = format_insights(generate_insights(str(sessions), days=7))
    assert "Insights" in out and "smartup_search_products" in out

    empty = format_insights({"days": 7, "empty": True})
    assert "ma'lumot yo'q" in empty
