"""Tests for WAL protocol and memory system."""

from __future__ import annotations

import pytest
from pathlib import Path

from qanot.memory import wal_scan, wal_write, write_daily_note, memory_search


class TestWalScan:
    def test_empty_message(self):
        assert wal_scan("") == []
        assert wal_scan("   ") == []

    def test_correction_trigger(self):
        entries = wal_scan("Actually, the server is on port 8080")
        assert len(entries) >= 1
        assert any(e.category == "correction" for e in entries)

    def test_proper_noun_trigger(self):
        entries = wal_scan("My name is Sardor")
        assert any(e.category == "proper_noun" for e in entries)

    def test_preference_trigger(self):
        entries = wal_scan("I prefer dark mode")
        assert any(e.category == "preference" for e in entries)

    def test_decision_trigger(self):
        entries = wal_scan("Let's use PostgreSQL for this")
        assert any(e.category == "decision" for e in entries)

    def test_specific_value_url(self):
        entries = wal_scan("Check https://example.com/api for details")
        assert any(e.category == "specific_value" for e in entries)

    def test_specific_value_date(self):
        entries = wal_scan("The deadline is 2025-03-15")
        assert any(e.category == "specific_value" for e in entries)

    def test_no_triggers(self):
        entries = wal_scan("How is the weather today?")
        assert entries == []


class TestWalWrite:
    def test_creates_session_state(self, tmp_path):
        entries = wal_scan("Actually, we need Redis")
        wal_write(entries, str(tmp_path))

        state_path = tmp_path / "SESSION-STATE.md"
        assert state_path.exists()
        content = state_path.read_text()
        assert "correction" in content

    def test_appends_to_existing(self, tmp_path):
        state_path = tmp_path / "SESSION-STATE.md"
        state_path.write_text("# SESSION-STATE.md\n\n")

        entries = wal_scan("I prefer Python over Go")
        wal_write(entries, str(tmp_path))

        content = state_path.read_text()
        assert "SESSION-STATE.md" in content
        assert "preference" in content

    def test_empty_entries_noop(self, tmp_path):
        wal_write([], str(tmp_path))
        assert not (tmp_path / "SESSION-STATE.md").exists()


class TestDailyNote:
    def test_creates_daily_note(self, tmp_path):
        write_daily_note("Test note content", str(tmp_path))

        memory_dir = tmp_path / "memory"
        assert memory_dir.exists()
        notes = list(memory_dir.glob("*.md"))
        assert len(notes) == 1

        content = notes[0].read_text()
        assert "Test note content" in content

    def test_appends_to_existing_note(self, tmp_path):
        write_daily_note("First entry", str(tmp_path))
        write_daily_note("Second entry", str(tmp_path))

        memory_dir = tmp_path / "memory"
        notes = list(memory_dir.glob("*.md"))
        assert len(notes) == 1

        content = notes[0].read_text()
        assert "First entry" in content
        assert "Second entry" in content


class TestMemorySearch:
    def test_search_memory_md(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("# Memory\n\nRemember: user likes Python\n")
        results = memory_search("python", str(tmp_path))
        assert len(results) >= 1
        assert any("Python" in r["content"] for r in results)

    def test_search_daily_notes(self, tmp_path):
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / "2025-03-01.md").write_text("# Notes\n\nDeployed to production\n")
        results = memory_search("production", str(tmp_path))
        assert len(results) >= 1

    def test_search_no_results(self, tmp_path):
        results = memory_search("nonexistent", str(tmp_path))
        assert results == []
