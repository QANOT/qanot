"""Tests for the backup rotation system."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from qanot.backup import (
    MAX_BACKUPS,
    BACKUP_FILES,
    backup_workspace,
    _find_config_path,
    _rotate_backups,
)


class TestBackupWorkspace:
    def test_backup_creates_directory(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "MEMORY.md").write_text("# Memory")
        (ws / "SOUL.md").write_text("# Soul")

        result = backup_workspace(str(ws))

        assert result is not None
        backup_dir = Path(result)
        assert backup_dir.is_dir()
        assert (backup_dir / "MEMORY.md").read_text() == "# Memory"
        assert (backup_dir / "SOUL.md").read_text() == "# Soul"

    def test_backup_copies_memory_directory(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        memory_dir = ws / "memory"
        memory_dir.mkdir()
        (memory_dir / "2026-03-11.md").write_text("day 1 notes")
        (memory_dir / "2026-03-12.md").write_text("day 2 notes")
        (ws / "MEMORY.md").write_text("# Memory")

        result = backup_workspace(str(ws))

        assert result is not None
        backup_dir = Path(result)
        assert (backup_dir / "memory" / "2026-03-11.md").read_text() == "day 1 notes"
        assert (backup_dir / "memory" / "2026-03-12.md").read_text() == "day 2 notes"

    def test_backup_skips_missing_files(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        # Only MEMORY.md exists, others are missing
        (ws / "MEMORY.md").write_text("# Memory")

        result = backup_workspace(str(ws))

        assert result is not None
        backup_dir = Path(result)
        assert (backup_dir / "MEMORY.md").is_file()
        assert not (backup_dir / "SESSION-STATE.md").exists()
        assert not (backup_dir / "IDENTITY.md").exists()

    def test_backup_returns_none_when_nothing_to_backup(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        # Empty workspace, no files to back up

        result = backup_workspace(str(ws))

        assert result is None

    def test_backup_returns_none_for_nonexistent_workspace(self, tmp_path: Path):
        result = backup_workspace(str(tmp_path / "nonexistent"))
        assert result is None

    def test_backup_includes_config_from_parent(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "MEMORY.md").write_text("# Memory")
        # Config in parent directory (Docker layout: /data/config.json, /data/workspace/)
        (tmp_path / "config.json").write_text('{"bot_token": "test"}')

        result = backup_workspace(str(ws))

        assert result is not None
        backup_dir = Path(result)
        assert (backup_dir / "config.json").is_file()

    def test_backup_includes_heartbeat_if_exists(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "MEMORY.md").write_text("# Memory")
        (ws / "HEARTBEAT.md").write_text("# Heartbeat\nLast: 2026-03-12")

        result = backup_workspace(str(ws))

        assert result is not None
        backup_dir = Path(result)
        assert (backup_dir / "HEARTBEAT.md").read_text() == "# Heartbeat\nLast: 2026-03-12"


class TestRotateBackups:
    def test_rotation_keeps_max_backups(self, tmp_path: Path):
        backups_root = tmp_path / "backups"
        backups_root.mkdir()

        # Create more than MAX_BACKUPS directories
        for i in range(MAX_BACKUPS + 3):
            d = backups_root / f"2026-03-{i+1:02d}T00-00-00"
            d.mkdir()
            (d / "MEMORY.md").write_text(f"backup {i}")

        _rotate_backups(backups_root)

        remaining = sorted(d.name for d in backups_root.iterdir() if d.is_dir())
        assert len(remaining) == MAX_BACKUPS
        # Oldest should be removed, newest kept
        assert remaining[-1] == f"2026-03-{MAX_BACKUPS + 3:02d}T00-00-00"

    def test_rotation_noop_when_under_limit(self, tmp_path: Path):
        backups_root = tmp_path / "backups"
        backups_root.mkdir()

        for i in range(3):
            d = backups_root / f"2026-03-{i+1:02d}T00-00-00"
            d.mkdir()

        _rotate_backups(backups_root)

        remaining = list(backups_root.iterdir())
        assert len(remaining) == 3

    def test_rotation_nonexistent_dir(self, tmp_path: Path):
        # Should not raise
        _rotate_backups(tmp_path / "nonexistent")


class TestFindConfigPath:
    def test_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        config_file = tmp_path / "custom_config.json"
        config_file.write_text('{"test": true}')
        monkeypatch.setenv("QANOT_CONFIG", str(config_file))

        result = _find_config_path(tmp_path / "workspace")
        assert result == config_file

    def test_from_parent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("QANOT_CONFIG", raising=False)
        ws = tmp_path / "workspace"
        ws.mkdir()
        config_file = tmp_path / "config.json"
        config_file.write_text('{"test": true}')

        result = _find_config_path(ws)
        assert result == config_file

    def test_from_workspace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("QANOT_CONFIG", raising=False)
        ws = tmp_path / "workspace"
        ws.mkdir()
        config_file = ws / "config.json"
        config_file.write_text('{"test": true}')

        result = _find_config_path(ws)
        assert result == config_file

    def test_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("QANOT_CONFIG", raising=False)
        ws = tmp_path / "workspace"
        ws.mkdir()

        result = _find_config_path(ws)
        assert result is None


class TestBackupIntegration:
    def test_full_backup_rotation_cycle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "MEMORY.md").write_text("# Memory")
        (ws / "SOUL.md").write_text("# Soul")

        # Pre-create MAX_BACKUPS + 2 backup directories to simulate accumulated backups
        backups_root = ws / "backups"
        backups_root.mkdir()
        for i in range(MAX_BACKUPS + 2):
            d = backups_root / f"2026-03-{i+1:02d}T00-00-00"
            d.mkdir()
            (d / "MEMORY.md").write_text(f"backup {i}")

        assert len(list(backups_root.iterdir())) == MAX_BACKUPS + 2

        # Next backup should trigger rotation
        result = backup_workspace(str(ws))
        assert result is not None

        # After rotation: MAX_BACKUPS dirs remain (oldest pruned)
        remaining = [d for d in backups_root.iterdir() if d.is_dir()]
        assert len(remaining) == MAX_BACKUPS

        # The new backup should still exist
        assert Path(result).is_dir()
