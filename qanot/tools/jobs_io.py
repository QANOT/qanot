"""Shared jobs.json I/O — used by both scheduler.py and tools/cron.py."""

from __future__ import annotations

import json
from pathlib import Path


# Maximum jobs file size: 10 MB (legitimate job lists are never this large)
_MAX_JOBS_FILE_BYTES = 10 * 1024 * 1024


def load_jobs(jobs_path: Path) -> list[dict]:
    """Load jobs from JSON file."""
    try:
        if jobs_path.stat().st_size > _MAX_JOBS_FILE_BYTES:
            return []
        with jobs_path.open(encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []


def save_jobs(jobs_path: Path, jobs: list[dict]) -> None:
    """Save jobs to JSON file (atomic write to prevent corruption)."""
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = jobs_path.with_suffix(jobs_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)
    tmp_path.replace(jobs_path)
