"""Tests for ContextTracker."""

from __future__ import annotations

import pytest

from qanot.context import ContextTracker


class TestContextTracker:
    def test_initial_state(self):
        ct = ContextTracker(max_tokens=100_000)
        assert ct.total_tokens == 0
        assert ct.get_context_percent() == 0.0
        assert ct.buffer_active is False

    def test_add_usage(self):
        ct = ContextTracker(max_tokens=100_000)
        ct.add_usage(1000, 500)
        assert ct.total_input == 1000
        assert ct.total_output == 500
        assert ct.total_tokens == 1500

    def test_context_percent(self):
        ct = ContextTracker(max_tokens=100_000)
        ct.add_usage(60_000, 0)
        assert ct.get_context_percent() == 60.0

    def test_threshold_activates_at_60(self, tmp_path):
        ct = ContextTracker(max_tokens=100_000, workspace_dir=str(tmp_path))
        ct.add_usage(59_000, 0)
        assert ct.check_threshold() is False
        assert ct.buffer_active is False

        ct.add_usage(1_000, 0)
        assert ct.check_threshold() is True
        assert ct.buffer_active is True

    def test_threshold_fires_once(self, tmp_path):
        ct = ContextTracker(max_tokens=100_000, workspace_dir=str(tmp_path))
        ct.add_usage(70_000, 0)
        assert ct.check_threshold() is True
        # Second call should return False (already active)
        assert ct.check_threshold() is False

    def test_working_buffer_file_created(self, tmp_path):
        ct = ContextTracker(max_tokens=100_000, workspace_dir=str(tmp_path))
        ct.add_usage(60_000, 0)
        ct.check_threshold()
        assert (tmp_path / "memory" / "working-buffer.md").exists()

    def test_append_to_buffer(self, tmp_path):
        ct = ContextTracker(max_tokens=100_000, workspace_dir=str(tmp_path))
        ct.add_usage(60_000, 0)
        ct.check_threshold()
        ct.append_to_buffer("User asked X", "Agent replied Y")

        content = (tmp_path / "memory" / "working-buffer.md").read_text()
        assert "User asked X" in content
        assert "Agent replied Y" in content

    def test_append_inactive_noop(self, tmp_path):
        ct = ContextTracker(max_tokens=100_000, workspace_dir=str(tmp_path))
        ct.append_to_buffer("ignored", "ignored")
        assert not (tmp_path / "memory" / "working-buffer.md").exists()

    def test_detect_compaction(self):
        ct = ContextTracker()
        assert ct.detect_compaction([]) is False
        assert ct.detect_compaction([{"content": "hello"}]) is False
        assert ct.detect_compaction([{"content": "<summary>old context</summary>"}]) is True
        assert ct.detect_compaction([{"content": "where were we?"}]) is True

    def test_recover_from_compaction(self, tmp_path):
        ct = ContextTracker(workspace_dir=str(tmp_path))

        # Create session state
        (tmp_path / "SESSION-STATE.md").write_text("Important state info")

        recovery = ct.recover_from_compaction()
        assert "Important state info" in recovery

    def test_session_status(self):
        ct = ContextTracker(max_tokens=200_000)
        ct.add_usage(50_000, 10_000)
        status = ct.session_status()
        assert status["total_tokens"] == 60_000
        assert status["context_percent"] == 25.0
        assert status["buffer_active"] is False

    def test_zero_max_tokens(self):
        ct = ContextTracker(max_tokens=0)
        assert ct.get_context_percent() == 0.0
