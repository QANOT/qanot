"""Persistent sub-agent run registry.

Tracks all sub-agent runs in memory with JSON disk persistence.
Inspired by OpenClaw's subagent-registry but much simpler.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from qanot.orchestrator.types import SubagentRun, TERMINAL_STATUSES

logger = logging.getLogger(__name__)

# Stale run cleanup: runs older than this are evicted
DEFAULT_MAX_AGE = 3600  # 1 hour
# Maximum runs to keep in memory
MAX_REGISTRY_SIZE = 200


class SubagentRegistry:
    """In-memory registry with optional JSON disk persistence."""

    def __init__(self, persist_path: Path | str | None = None):
        self._runs: dict[str, SubagentRun] = {}
        self._by_user: dict[str, list[str]] = {}  # user_id -> [run_ids]
        self._persist_path = Path(persist_path) if persist_path else None

    def register(self, run: SubagentRun) -> None:
        """Register a new run."""
        self._runs[run.run_id] = run
        user_runs = self._by_user.setdefault(run.parent_user_id, [])
        user_runs.append(run.run_id)
        self._persist()

    def update(self, run_id: str, **kwargs: Any) -> SubagentRun | None:
        """Update fields on an existing run. Returns updated run or None."""
        run = self._runs.get(run_id)
        if run is None:
            return None
        for key, value in kwargs.items():
            if hasattr(run, key):
                setattr(run, key, value)
        self._persist()
        return run

    def get(self, run_id: str) -> SubagentRun | None:
        return self._runs.get(run_id)

    def get_active_for_user(self, user_id: str) -> list[SubagentRun]:
        """Get all non-terminal runs for a user."""
        run_ids = self._by_user.get(user_id, [])
        return [
            self._runs[rid]
            for rid in run_ids
            if rid in self._runs and not self._runs[rid].is_terminal
        ]

    def get_recent_for_user(self, user_id: str, limit: int = 10) -> list[SubagentRun]:
        """Get most recent runs for a user (any status)."""
        run_ids = self._by_user.get(user_id, [])
        runs = [self._runs[rid] for rid in run_ids if rid in self._runs]
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    def count_active_for_user(self, user_id: str) -> int:
        return len(self.get_active_for_user(user_id))

    def cleanup_stale(self, max_age: float = DEFAULT_MAX_AGE) -> int:
        """Remove terminal runs older than max_age. Returns count removed."""
        now = time.time()
        to_remove: list[str] = []
        for run_id, run in self._runs.items():
            if run.is_terminal and (now - run.created_at) > max_age:
                to_remove.append(run_id)

        for run_id in to_remove:
            run = self._runs.pop(run_id, None)
            if run:
                user_runs = self._by_user.get(run.parent_user_id, [])
                if run_id in user_runs:
                    user_runs.remove(run_id)

        if to_remove:
            self._persist()
            logger.debug("Cleaned up %d stale runs", len(to_remove))
        return len(to_remove)

    def persist(self) -> None:
        """Force persist to disk (public API)."""
        self._persist()

    def restore(self) -> None:
        """Restore registry from disk on startup."""
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text())
            for entry in data.get("runs", []):
                try:
                    run = SubagentRun.from_dict(entry)
                    # Mark non-terminal runs as failed (orphaned by restart)
                    if not run.is_terminal:
                        run.status = "failed"
                        run.error = "Orphaned by process restart"
                        run.ended_at = time.time()
                    self._runs[run.run_id] = run
                    self._by_user.setdefault(run.parent_user_id, []).append(run.run_id)
                except Exception as e:
                    logger.warning("Skipping corrupt registry entry: %s", e)
            logger.info("Restored %d runs from registry", len(self._runs))
        except Exception as e:
            logger.warning("Failed to restore registry: %s", e)

    def _persist(self) -> None:
        """Write registry to disk (internal)."""
        if not self._persist_path:
            return

        # Enforce size limit
        if len(self._runs) > MAX_REGISTRY_SIZE:
            self.cleanup_stale(max_age=1800)  # 30 min for aggressive cleanup

        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"runs": [r.to_dict() for r in self._runs.values()]}
            # Atomic write
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, default=str))
            tmp.replace(self._persist_path)
        except Exception as e:
            logger.warning("Failed to persist registry: %s", e)
