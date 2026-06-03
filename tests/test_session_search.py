"""Tests for cross-session transcript search (qanot/session_search.py)."""

from __future__ import annotations

import json

import pytest

from qanot.session_search import SessionSearchIndex, _entry_text, _to_fts_query


def _write_session(sessions_dir, date_str, entries):
    p = sessions_dir / f"{date_str}.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return p


def _user(text, uid="42"):
    return {"type": "message", "message": {"role": "user", "content": text},
            "timestamp": "2026-05-12T10:00:00Z", "user_id": uid}


def _assistant(text):
    return {"type": "message",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            "timestamp": "2026-05-12T10:00:05Z"}


def test_entry_text_extraction():
    role, text, ts, uid = _entry_text(_user("salom dunyo", uid="7"))
    assert role == "user" and text == "salom dunyo" and uid == "7"
    role, text, _, _ = _entry_text(_assistant("javob matni"))
    assert role == "assistant" and "javob matni" in text


def test_to_fts_query_quotes_terms():
    assert _to_fts_query('bambuk narx') == '"bambuk" "narx"'
    # FTS operators in user text can't break the query
    assert _to_fts_query('a OR b" NEAR') == '"a" "OR" "b" "NEAR"'
    assert _to_fts_query('') == ''


def test_index_and_search(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_session(sessions, "2026-05-10", [
        _user("bambuk tovarlari haqida gaplashdik"),
        _assistant("bambuk kategoriyasida 35 mahsulot bor"),
    ])
    _write_session(sessions, "2026-05-12", [
        _user("telefon narxlari qancha"),
        _assistant("iphone 15 narxi ..."),
    ])
    idx = SessionSearchIndex(str(sessions), str(tmp_path / "fts.db"))
    n = idx.sync()
    assert n == 2

    res = idx.search("bambuk")
    assert res["total_matches"] >= 1
    dates = [s["date"] for s in res["sessions"]]
    assert "2026-05-10" in dates
    # the match snippet mentions bambuk
    assert any("bambuk" in m["snippet"].lower()
               for s in res["sessions"] for m in s["matches"])

    # unrelated query in a different session
    res2 = idx.search("telefon")
    assert any(s["date"] == "2026-05-12" for s in res2["sessions"])


def test_search_no_match(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_session(sessions, "2026-05-10", [_user("salom")])
    idx = SessionSearchIndex(str(sessions), str(tmp_path / "fts.db"))
    res = idx.search("kvant fizikasi")
    assert res["sessions"] == []
    assert res["total_matches"] == 0


def test_incremental_reindex_only_changed(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_session(sessions, "2026-05-10", [_user("birinchi xabar")])
    idx = SessionSearchIndex(str(sessions), str(tmp_path / "fts.db"))
    assert idx.sync() == 1
    assert idx.sync() == 0  # nothing changed → no re-index

    # append to today's file → only that file re-indexes
    import time
    time.sleep(0.01)
    _write_session(sessions, "2026-05-10", [_user("birinchi xabar"), _user("ikkinchi xabar yangi")])
    assert idx.sync() == 1
    res = idx.search("ikkinchi")
    assert res["total_matches"] >= 1


def test_user_scoping(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_session(sessions, "2026-05-10", [
        _user("maxfiy reja", uid="111"),
        _user("maxfiy reja boshqa", uid="222"),
    ])
    idx = SessionSearchIndex(str(sessions), str(tmp_path / "fts.db"))
    idx.sync()
    res = idx.search("maxfiy", user_id="111")
    # only user 111's message matched
    assert res["total_matches"] == 1
