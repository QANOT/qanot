"""Tests for WAL protocol and memory system (OpenClaw-style shared memory)."""

from __future__ import annotations

import pytest
from pathlib import Path

from qanot.memory import wal_scan, wal_write, write_daily_note, memory_search

TEST_USER = "12345"


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
    def test_creates_shared_session_state(self, tmp_path):
        """WAL writes to shared SESSION-STATE.md at workspace root."""
        entries = wal_scan("Actually, we need Redis")
        wal_write(entries, str(tmp_path), user_id=TEST_USER)

        state_path = tmp_path / "SESSION-STATE.md"
        assert state_path.exists()
        content = state_path.read_text()
        assert "correction" in content
        assert f"user:{TEST_USER}" in content

    def test_appends_to_existing(self, tmp_path):
        state_path = tmp_path / "SESSION-STATE.md"
        state_path.write_text("# SESSION-STATE.md\n\n")

        entries = wal_scan("I prefer Python over Go")
        wal_write(entries, str(tmp_path), user_id=TEST_USER)

        content = state_path.read_text()
        assert "SESSION-STATE.md" in content
        assert "preference" in content

    def test_empty_entries_noop(self, tmp_path):
        wal_write([], str(tmp_path), user_id=TEST_USER)
        assert not (tmp_path / "SESSION-STATE.md").exists()

    def test_shared_memory_across_users(self, tmp_path):
        """All users write to same shared MEMORY.md at workspace root."""
        entries_a = wal_scan("My name is Alice")
        wal_write(entries_a, str(tmp_path), user_id="user_a")

        entries_b = wal_scan("My name is Boris")
        wal_write(entries_b, str(tmp_path), user_id="user_b")

        shared_memory = tmp_path / "MEMORY.md"
        assert shared_memory.exists()
        content = shared_memory.read_text()
        assert "Alice" in content
        assert "Boris" in content
        # Both tagged with their user_ids
        assert "user:user_a" in content
        assert "user:user_b" in content

    def test_user_tagged_in_shared_state(self, tmp_path):
        """Shared SESSION-STATE entries are tagged with user_id."""
        entries = wal_scan("Actually, port is 3000")
        wal_write(entries, str(tmp_path), user_id="user_42")

        content = (tmp_path / "SESSION-STATE.md").read_text()
        assert "[user:user_42]" in content


class TestDailyNote:
    def test_creates_shared_daily_note(self, tmp_path):
        """Daily notes go to workspace/memory/ (shared, not per-user)."""
        write_daily_note("Test note content", str(tmp_path), user_id=TEST_USER)

        memory_dir = tmp_path / "memory"
        assert memory_dir.exists()
        notes = list(memory_dir.glob("*.md"))
        assert len(notes) == 1

        content = notes[0].read_text()
        assert "Test note content" in content
        assert f"user:{TEST_USER}" in content

    def test_appends_to_existing_note(self, tmp_path):
        write_daily_note("First entry", str(tmp_path), user_id=TEST_USER)
        write_daily_note("Second entry", str(tmp_path), user_id=TEST_USER)

        memory_dir = tmp_path / "memory"
        notes = list(memory_dir.glob("*.md"))
        assert len(notes) == 1

        content = notes[0].read_text()
        assert "First entry" in content
        assert "Second entry" in content

    def test_multiple_users_same_file(self, tmp_path):
        """All users' notes go to the same daily file (per-agent)."""
        write_daily_note("User A spoke", str(tmp_path), user_id="user_a")
        write_daily_note("User B spoke", str(tmp_path), user_id="user_b")

        memory_dir = tmp_path / "memory"
        notes = list(memory_dir.glob("*.md"))
        assert len(notes) == 1  # Same file, not separate

        content = notes[0].read_text()
        assert "User A spoke" in content
        assert "User B spoke" in content
        assert "user:user_a" in content
        assert "user:user_b" in content


class TestMemorySearch:
    def test_search_shared_memory(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("# Memory\n\nRemember: user likes Python\n")
        results = memory_search("python", str(tmp_path), user_id=TEST_USER)
        assert len(results) >= 1
        assert any("Python" in r["content"] for r in results)

    def test_search_shared_daily_notes(self, tmp_path):
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "2025-03-01.md").write_text("# Notes\n\nDeployed to production\n")
        results = memory_search("production", str(tmp_path), user_id=TEST_USER)
        assert len(results) >= 1

    def test_search_no_results(self, tmp_path):
        results = memory_search("nonexistent", str(tmp_path), user_id=TEST_USER)
        assert results == []

    def test_all_users_see_shared_memory(self, tmp_path):
        """Any user can find facts from shared MEMORY.md."""
        (tmp_path / "MEMORY.md").write_text(
            "# Memory\n\n"
            "- [user:user_a] My name is Alice\n"
            "- [user:user_b] My name is Boris\n"
        )

        # User A sees everything
        results_a = memory_search("Alice", str(tmp_path), user_id="user_a")
        assert len(results_a) >= 1

        # User B also sees User A's facts (shared memory)
        results_b = memory_search("Alice", str(tmp_path), user_id="user_b")
        assert len(results_b) >= 1
