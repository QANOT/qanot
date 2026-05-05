"""Unit tests for the self-improvement learnings store + tools.

Covers:
  - JSONL persistence + ordering (newest first on load)
  - Validation: required fields, length caps, malformed input
  - Pruning when entries exceed MAX_ENTRIES_ON_DISK
  - Search: substring match across observation + lesson + tags
  - Prompt injection blocks: empty when no learnings, formatted when populated
  - Error-lesson extraction from daily notes
  - Tool handlers: success path + error path
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qanot import learnings as L
from qanot.registry import ToolRegistry
from qanot.tools.learnings import register_learning_tools


# ── append + load ──────────────────────────────────────────────


def test_append_and_load_roundtrip(tmp_path: Path):
    L.append_learning(str(tmp_path), "saw something", "do X next time", tags=["a", "b"])
    L.append_learning(str(tmp_path), "saw second", "do Y", tags=["c"])

    entries = L.load_learnings(str(tmp_path))
    assert len(entries) == 2
    # newest first
    assert entries[0]["lesson"] == "do Y"
    assert entries[1]["lesson"] == "do X next time"
    assert entries[0]["tags"] == ["c"]
    assert entries[1]["tags"] == ["a", "b"]
    # ts is ISO8601 with timezone
    assert "T" in entries[0]["ts"] and ("+" in entries[0]["ts"] or entries[0]["ts"].endswith("Z"))


def test_append_creates_memory_dir(tmp_path: Path):
    assert not (tmp_path / "memory").exists()
    L.append_learning(str(tmp_path), "x", "y")
    assert (tmp_path / "memory").is_dir()
    assert (tmp_path / "memory" / "learnings.jsonl").is_file()


def test_load_returns_empty_when_no_file(tmp_path: Path):
    assert L.load_learnings(str(tmp_path)) == []


def test_limit_caps_load_count(tmp_path: Path):
    for i in range(5):
        L.append_learning(str(tmp_path), f"obs{i}", f"lesson{i}")
    out = L.load_learnings(str(tmp_path), limit=2)
    assert len(out) == 2
    # Newest two — lesson4 and lesson3
    assert out[0]["lesson"] == "lesson4"
    assert out[1]["lesson"] == "lesson3"


# ── validation ─────────────────────────────────────────────────


def test_empty_observation_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="observation"):
        L.append_learning(str(tmp_path), "", "lesson")


def test_empty_lesson_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="lesson"):
        L.append_learning(str(tmp_path), "obs", "")


def test_observation_too_long_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="observation too long"):
        L.append_learning(str(tmp_path), "x" * 501, "lesson")


def test_lesson_too_long_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="lesson too long"):
        L.append_learning(str(tmp_path), "obs", "y" * 401)


def test_tags_normalize_lowercase_and_dedupe(tmp_path: Path):
    L.append_learning(str(tmp_path), "obs", "lesson", tags=["Alpha", "alpha", "BETA", " beta "])
    e = L.load_learnings(str(tmp_path))[0]
    assert e["tags"] == ["alpha", "beta"]


def test_tags_capped_at_ten(tmp_path: Path):
    tags = [f"tag{i}" for i in range(20)]
    L.append_learning(str(tmp_path), "obs", "lesson", tags=tags)
    e = L.load_learnings(str(tmp_path))[0]
    assert len(e["tags"]) == 10


def test_corrupt_jsonl_lines_are_skipped(tmp_path: Path):
    L.append_learning(str(tmp_path), "good", "lesson1")
    # Inject corrupt content into the file
    p = tmp_path / "memory" / "learnings.jsonl"
    with open(p, "a") as f:
        f.write("this is not json\n")
        f.write('{"incomplete":\n')
    L.append_learning(str(tmp_path), "good2", "lesson2")
    entries = L.load_learnings(str(tmp_path))
    assert {e["lesson"] for e in entries} == {"lesson1", "lesson2"}


# ── pruning ────────────────────────────────────────────────────


def test_pruning_keeps_newest(tmp_path: Path, monkeypatch):
    # Lower the cap for the test to keep it fast.
    monkeypatch.setattr(L, "MAX_ENTRIES_ON_DISK", 5)
    for i in range(10):
        L.append_learning(str(tmp_path), f"obs{i}", f"lesson{i}")
    entries = L.load_learnings(str(tmp_path))
    assert len(entries) == 5
    # Should be lesson9, lesson8, lesson7, lesson6, lesson5 (newest first)
    assert [e["lesson"] for e in entries] == [f"lesson{i}" for i in (9, 8, 7, 6, 5)]


# ── search ─────────────────────────────────────────────────────


def test_search_substring_matches_lesson_field(tmp_path: Path):
    L.append_learning(str(tmp_path), "obs1", "use absmarket_get_cashier_daily_report for cashier")
    L.append_learning(str(tmp_path), "obs2", "topkey history is precise wall-clock")
    L.append_learning(str(tmp_path), "obs3", "always check the docs first")
    matches = L.search_learnings(str(tmp_path), "cashier")
    assert len(matches) == 1
    assert matches[0]["observation"] == "obs1"


def test_search_matches_tags(tmp_path: Path):
    L.append_learning(str(tmp_path), "o", "l1", tags=["absmarket", "report"])
    L.append_learning(str(tmp_path), "o", "l2", tags=["topkey"])
    matches = L.search_learnings(str(tmp_path), "absmarket")
    assert len(matches) == 1
    assert matches[0]["lesson"] == "l1"


def test_search_empty_topic_returns_recent(tmp_path: Path):
    for i in range(3):
        L.append_learning(str(tmp_path), f"obs{i}", f"lesson{i}")
    matches = L.search_learnings(str(tmp_path), "", limit=2)
    assert len(matches) == 2
    assert matches[0]["lesson"] == "lesson2"


def test_search_respects_limit(tmp_path: Path):
    for i in range(5):
        L.append_learning(str(tmp_path), "obs", f"common-keyword lesson {i}")
    matches = L.search_learnings(str(tmp_path), "common-keyword", limit=3)
    assert len(matches) == 3


# ── prompt blocks ──────────────────────────────────────────────


def test_format_recent_learnings_block_empty(tmp_path: Path):
    assert L.format_recent_learnings_block(str(tmp_path)) == ""


def test_format_recent_learnings_block_populated(tmp_path: Path):
    L.append_learning(str(tmp_path), "obs", "this is the rule", tags=["x"])
    block = L.format_recent_learnings_block(str(tmp_path))
    assert "Recent Learnings" in block
    assert "this is the rule" in block
    assert "[x]" in block


def test_format_recent_learnings_compact_size(tmp_path: Path):
    """5 entries should produce a compact block — bound on prompt cost."""
    for i in range(5):
        L.append_learning(str(tmp_path), f"obs{i}", f"lesson number {i}", tags=[f"t{i}"])
    block = L.format_recent_learnings_block(str(tmp_path))
    # ~150 tokens budget = ~600 chars
    assert len(block) < 1200, f"block too large: {len(block)} chars"


# ── error lesson extraction ────────────────────────────────────


def test_extract_error_lessons_empty_when_no_notes(tmp_path: Path):
    assert L.extract_recent_error_lessons(str(tmp_path)) == []


def test_extract_error_lessons_finds_marked_entries(tmp_path: Path):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    notes_dir = tmp_path / "memory"
    notes_dir.mkdir()
    (notes_dir / f"{today}.md").write_text(
        f"# Daily Notes — {today}\n\n"
        "## [10:00:00]\nNormal turn summary.\n\n"
        "## [11:00:00]\nLoop detected: same tool same input three times. Rolling back.\n\n"
        "## [12:00:00]\nAnother normal turn.\n\n"
        "## [13:00:00]\nError lesson: never call topkey_query without del_status filter.\n",
        encoding="utf-8",
    )
    out = L.extract_recent_error_lessons(str(tmp_path))
    assert len(out) >= 2
    contents = " ".join(e["content"] for e in out).lower()
    assert "loop detected" in contents
    assert "error lesson" in contents


def test_format_error_lessons_block_empty(tmp_path: Path):
    assert L.format_error_lessons_block(str(tmp_path)) == ""


# ── tool handlers ──────────────────────────────────────────────


def test_evolve_soul_tool_success(tmp_path: Path):
    registry = ToolRegistry()
    register_learning_tools(registry, str(tmp_path), get_user_id=lambda: "user42")
    handler = registry.get_handler("evolve_soul")
    assert handler is not None

    result = asyncio.run(handler({
        "observation": "User asked twice — bot gave different answers",
        "lesson": "Use canonical tools instead of re-deriving via SQL",
        "tags": ["consistency", "absmarket"],
    }))
    parsed = json.loads(result)
    assert parsed["success"] is True
    assert parsed["lesson"] == "Use canonical tools instead of re-deriving via SQL"
    assert "consistency" in parsed["tags"]

    # File now contains the entry
    entries = L.load_learnings(str(tmp_path))
    assert len(entries) == 1
    assert entries[0]["user_id"] == "user42"


def test_evolve_soul_tool_validation_error_returns_envelope(tmp_path: Path):
    registry = ToolRegistry()
    register_learning_tools(registry, str(tmp_path))
    handler = registry.get_handler("evolve_soul")
    assert handler is not None
    result = asyncio.run(handler({"observation": "", "lesson": "lesson"}))
    parsed = json.loads(result)
    assert "error" in parsed
    assert "observation" in parsed["error"]


def test_evolve_soul_rejects_non_list_tags(tmp_path: Path):
    registry = ToolRegistry()
    register_learning_tools(registry, str(tmp_path))
    handler = registry.get_handler("evolve_soul")
    assert handler is not None
    result = asyncio.run(handler({
        "observation": "obs", "lesson": "lesson", "tags": "not a list",
    }))
    parsed = json.loads(result)
    assert "error" in parsed
    assert "tags" in parsed["error"]


def test_recall_lessons_tool_returns_matches(tmp_path: Path):
    L.append_learning(str(tmp_path), "obs1", "use canonical tool for cashier", tags=["absmarket"])
    L.append_learning(str(tmp_path), "obs2", "topkey history is precise", tags=["topkey"])

    registry = ToolRegistry()
    register_learning_tools(registry, str(tmp_path))
    handler = registry.get_handler("recall_lessons")
    assert handler is not None

    result = asyncio.run(handler({"topic": "cashier"}))
    parsed = json.loads(result)
    assert parsed["count"] == 1
    assert "cashier" in parsed["lessons"][0]["lesson"]


def test_recall_lessons_default_returns_recent(tmp_path: Path):
    for i in range(3):
        L.append_learning(str(tmp_path), f"o{i}", f"lesson {i}")
    registry = ToolRegistry()
    register_learning_tools(registry, str(tmp_path))
    handler = registry.get_handler("recall_lessons")
    assert handler is not None
    result = asyncio.run(handler({}))
    parsed = json.loads(result)
    assert parsed["count"] == 3
    # Newest first
    assert parsed["lessons"][0]["lesson"] == "lesson 2"


def test_recall_lessons_clamps_limit(tmp_path: Path):
    for i in range(30):
        L.append_learning(str(tmp_path), f"o{i}", f"lesson {i}")
    registry = ToolRegistry()
    register_learning_tools(registry, str(tmp_path))
    handler = registry.get_handler("recall_lessons")
    assert handler is not None
    # Asking for 999 — clamps to 20
    result = asyncio.run(handler({"limit": 999}))
    parsed = json.loads(result)
    assert parsed["count"] == 20
