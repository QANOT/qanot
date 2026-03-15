"""Backup rotation system for critical workspace files."""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_BACKUPS = 5  # Keep last 5 backups

# Files to back up (relative to workspace_dir)
BACKUP_FILES = [
    "MEMORY.md",
    "SESSION-STATE.md",
    "IDENTITY.md",
    "USER.md",
    "SOUL.md",
    "HEARTBEAT.md",
]

# Directory to back up (relative to workspace_dir)
BACKUP_DIRS = [
    "memory",
]


def backup_workspace(workspace_dir: str) -> str | None:
    """Create a backup of critical workspace files.

    Backs up: MEMORY.md, SESSION-STATE.md, IDENTITY.md, USER.md, SOUL.md,
    HEARTBEAT.md (if exists), memory/ directory, and config.json.

    Into: {workspace_dir}/backups/{timestamp}/

    Rotates: keeps only last MAX_BACKUPS directories.
    Returns backup path or None if nothing to back up.
    """
    ws = Path(workspace_dir)
    if not ws.is_dir():
        logger.warning("Workspace directory does not exist: %s", workspace_dir)
        return None

    # Collect files and directories that actually exist
    files_to_backup = [p for f in BACKUP_FILES if (p := ws / f).is_file()]
    dirs_to_backup = [p for d in BACKUP_DIRS if (p := ws / d).is_dir()]

    # Find config.json (from parent or QANOT_CONFIG env)
    config_path = _find_config_path(ws)
    if config_path and config_path.is_file():
        files_to_backup.append(config_path)

    if not files_to_backup and not dirs_to_backup:
        logger.info("No workspace files to back up")
        return None

    # Create backup directory with timestamp
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    backups_root = ws / "backups"
    backup_dir = backups_root / timestamp

    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Failed to create backup directory: %s", e)
        return None

    # Copy files
    copied = 0
    for fpath in files_to_backup:
        try:
            dest = backup_dir / fpath.name
            shutil.copy2(fpath, dest)
            copied += 1
        except OSError as e:
            logger.warning("Failed to backup %s: %s", fpath.name, e)

    # Copy directories
    for dpath in dirs_to_backup:
        try:
            dest = backup_dir / dpath.name
            shutil.copytree(dpath, dest, dirs_exist_ok=True)
            copied += 1
        except OSError as e:
            logger.warning("Failed to backup directory %s: %s", dpath.name, e)

    if copied == 0:
        # Nothing was actually copied, remove empty backup dir
        try:
            backup_dir.rmdir()
        except OSError:
            pass
        return None

    logger.info("Backup created: %s (%d items)", backup_dir, copied)

    # Rotate old backups
    _rotate_backups(backups_root)

    return str(backup_dir)


def _find_config_path(workspace_dir: Path) -> Path | None:
    """Locate config.json from QANOT_CONFIG env or parent directory."""
    # Check environment variable first
    env_path = os.environ.get("QANOT_CONFIG")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p

    # Check parent directory (common Docker layout: /data/config.json, /data/workspace/)
    parent_config = workspace_dir.parent / "config.json"
    if parent_config.is_file():
        return parent_config

    # Check workspace itself
    ws_config = workspace_dir / "config.json"
    if ws_config.is_file():
        return ws_config

    return None


_TS_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}$")


def _rotate_backups(backups_root: Path) -> None:
    """Keep only the last MAX_BACKUPS backup directories."""
    if not backups_root.is_dir():
        return

    try:
        # List backup directories, sorted alphabetically (timestamps sort chronologically)
        backup_dirs = sorted(
            [
                d
                for d in backups_root.iterdir()
                if d.is_dir() and not d.is_symlink() and _TS_PATTERN.match(d.name)
            ],
            key=lambda d: d.name,
        )

        # Remove oldest if over limit
        if len(backup_dirs) > MAX_BACKUPS:
            to_remove = backup_dirs[:-MAX_BACKUPS]
            for oldest in to_remove:
                try:
                    shutil.rmtree(oldest)
                    logger.info("Rotated old backup: %s", oldest.name)
                except OSError as e:
                    logger.warning("Failed to remove old backup %s: %s", oldest.name, e)
    except OSError as e:
        logger.warning("Backup rotation failed: %s", e)
