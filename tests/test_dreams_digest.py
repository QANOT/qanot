"""Tests for qanot.dreams.digest — deterministic session-transcript digest."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qanot.dreams.digest import build_session_digest, write_session_digest

NOW = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


def _write_session(dirp: Path, name: str, entries: list[dict], *, age_days: float = 0.0) -> Path:
    p = dirp / f"{name}.jsonl"
    p.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )
    if age_days:
        ts = (NOW - timedelta(days=age_days)).timestamp()
        os.utime(p, (ts, ts))
    else:
        ts = NOW.timestamp()
        os.utime(p, (ts, ts))
    return p


def _user(text: str, uid: str = "u1", ts: str = "2026-05-19T09:00:00+00:00") -> dict:
    return {"type": "message", "timestamp": ts, "user_id": uid,
            "message": {"role": "user", "content": text}}


def _asst(text: str, tools: list[str] | None = None, ts: str = "2026-05-19T09:00:05+00:00") -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    for t in tools or []:
        content.append({"type": "tool_use", "name": t, "input": {}})
    return {"type": "message", "timestamp": ts, "model": "claude-opus-4-7",
            "message": {"role": "assistant", "content": content}}


def test_missing_dir_returns_empty(tmp_path):
    assert build_session_digest(tmp_path / "nope", now=NOW) == ""


def test_empty_dir_returns_empty(tmp_path):
    assert build_session_digest(tmp_path, now=NOW) == ""


def test_basic_extraction(tmp_path):
    _write_session(tmp_path, "2026-05-19", [
        _user("har doim hisobotni 14:00 da yubor"),
        _asst("Tushundim, har kuni 14:00 da yuboraman.", tools=["cron_create"]),
    ])
    out = build_session_digest(tmp_path, now=NOW)
    assert "## 2026-05-19" in out
    assert "har doim hisobotni 14:00 da yubor" in out
    assert "User(u1)" in out
    assert "[tools: cron_create]" in out
    assert "Recent session transcripts" in out


def test_non_message_and_malformed_lines_skipped(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        json.dumps({"type": "session_meta", "x": 1}) + "\n"
        + "{not valid json\n"
        + json.dumps(_user("real message here")) + "\n",
        encoding="utf-8",
    )
    os.utime(p, (NOW.timestamp(), NOW.timestamp()))
    out = build_session_digest(tmp_path, now=NOW)
    assert "real message here" in out
    assert "session_meta" not in out


def test_cron_sessions_skipped(tmp_path):
    _write_session(tmp_path, "cron-heartbeat-20260519", [_user("internal cron noise")])
    _write_session(tmp_path, "2026-05-19", [_user("real user turn")])
    out = build_session_digest(tmp_path, now=NOW)
    assert "real user turn" in out
    assert "internal cron noise" not in out


def test_old_sessions_filtered_by_recency(tmp_path):
    _write_session(tmp_path, "old", [_user("ancient history")], age_days=30)
    _write_session(tmp_path, "fresh", [_user("recent stuff")], age_days=1)
    out = build_session_digest(tmp_path, days=7, now=NOW)
    assert "recent stuff" in out
    assert "ancient history" not in out


def test_per_message_cap_truncates_long_turn(tmp_path):
    _write_session(tmp_path, "2026-05-19", [_user("x" * 5000)])
    out = build_session_digest(tmp_path, now=NOW)
    assert "…" in out
    # the giant run must not survive in full
    assert "x" * 1000 not in out


def test_char_budget_keeps_newest_first(tmp_path):
    _write_session(tmp_path, "older", [_user("OLDER_MARKER aaa")], age_days=3)
    _write_session(tmp_path, "newest", [_user("NEWEST_MARKER bbb")], age_days=0)
    # Budget that fits the header + exactly the newest block, but not
    # both — proves newest-first ordering under the size guarantee.
    solo = tmp_path / "solo"
    solo.mkdir()
    _write_session(solo, "newest", [_user("NEWEST_MARKER bbb")], age_days=0)
    budget = len(build_session_digest(solo, now=NOW)) + 30
    out = build_session_digest(tmp_path, max_chars=budget, now=NOW)
    assert "NEWEST_MARKER" in out
    assert "OLDER_MARKER" not in out
    assert "older sessions omitted" in out


def test_session_with_no_extractable_turns_yields_empty(tmp_path):
    # assistant message with neither text nor tools → nothing to show
    _write_session(tmp_path, "blank", [
        {"type": "message", "timestamp": "2026-05-19T09:00:00+00:00",
         "message": {"role": "assistant", "content": []}},
    ])
    assert build_session_digest(tmp_path, now=NOW) == ""


def test_system_reminder_stripped_from_user_turns(tmp_path):
    reminder = (
        "<system-reminder>The following memories were recalled... "
        "feedback-title-format-rules-hard ...</system-reminder>"
    )
    _write_session(tmp_path, "2026-05-19", [
        _user(reminder + " menga hisobot kerak"),
        _user(reminder),  # nothing but scaffolding → must be dropped entirely
    ])
    out = build_session_digest(tmp_path, now=NOW)
    assert "menga hisobot kerak" in out
    assert "system-reminder" not in out
    assert "feedback-title-format-rules-hard" not in out
    # the reminder-only turn contributes no line
    assert out.count("- [") == 1


def test_system_reminder_stripped_from_assistant_echo(tmp_path):
    """Assistant turns echo prior context with nested <system-reminder>
    (memory-search results, compaction replays) — must also be stripped."""
    _write_session(tmp_path, "2026-05-19", [
        _user("davom et"),
        _asst(
            "Avvalgi kontekst: User: <system-reminder>recalled memo "
            "feedback-title-format</system-reminder> — endi javob: tayyor."
        ),
    ])
    out = build_session_digest(tmp_path, now=NOW)
    assert "system-reminder" not in out
    assert "feedback-title-format" not in out
    assert "endi javob: tayyor" in out


def test_per_session_cap_spreads_budget_across_sessions(tmp_path):
    """A chatty session must not starve the others — breadth matters
    for cross-session pattern mining."""
    chatty = [_user(f"turn {i} " + "z " * 80) for i in range(60)]
    _write_session(tmp_path, "chatty", chatty, age_days=0)
    _write_session(tmp_path, "older1", [_user("OLDER1 durable fact")], age_days=1)
    _write_session(tmp_path, "older2", [_user("OLDER2 durable fact")], age_days=2)
    out = build_session_digest(tmp_path, now=NOW)
    # all three sessions present despite the chatty one being huge
    assert out.count("## ") == 3
    assert "OLDER1 durable fact" in out
    assert "OLDER2 durable fact" in out
    assert "session clipped" in out


def test_write_session_digest_writes_and_counts(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    _write_session(sdir, "2026-05-19", [_user("persist this")])
    outp = tmp_path / "memory" / "recent-sessions-digest.md"
    n = write_session_digest(sdir, outp, now=NOW)
    assert n > 0
    assert outp.is_file()
    assert "persist this" in outp.read_text(encoding="utf-8")


def test_write_session_digest_noop_when_nothing(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    outp = tmp_path / "memory" / "recent-sessions-digest.md"
    n = write_session_digest(sdir, outp, now=NOW)
    assert n == 0
    assert not outp.exists()  # not created when there's nothing to digest
