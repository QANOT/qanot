"""Shared jobs.json I/O — used by both scheduler.py and tools/cron.py."""

from __future__ import annotations

import json
from pathlib import Path


def load_jobs(jobs_path: Path) -> list[dict]:
    """Load jobs from JSON file."""
    try:
        with jobs_path.open(encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_jobs(jobs_path: Path, jobs: list[dict]) -> None:
    """Save jobs to JSON file (atomic write to prevent corruption)."""
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = jobs_path.with_name(jobs_path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)
    tmp_path.replace(jobs_path)
