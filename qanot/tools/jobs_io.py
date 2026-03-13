"""Shared jobs.json I/O — used by both scheduler.py and tools/cron.py."""

from __future__ import annotations

import json
from pathlib import Path


def load_jobs(jobs_path: Path) -> list[dict]:
    """Load jobs from JSON file."""
    if jobs_path.exists():
        try:
            return json.loads(jobs_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def save_jobs(jobs_path: Path, jobs: list[dict]) -> None:
    """Save jobs to JSON file (atomic write to prevent corruption)."""
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = jobs_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(jobs_path)
