"""Persistent sub-agent run registry.

Tracks all sub-agent runs in memory with JSON disk persistence.
Inspired by OpenClaw's subagent-registry but much simpler.

Concurrency model:
- Mutating methods (register, update, cleanup_stale, persist) are async and
  serialize on `self._lock` (asyncio.Lock).
- Read methods (get, get_active_for_user, get_recent_for_user,
  count_active_for_user) are sync and assume callers tolerate eventual
  consistency. Each iterates a snapshot (list(...)) so a concurrent
  mutation can never raise "dictionary changed size during iteration".
- Disk I/O happens via `asyncio.to_thread(_persist_sync, snapshot, path)`
  so the event loop is never blocked on `tmp.write_text` / `tmp.replace`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from qanot.orchestrator.types import SubagentRun

logger = logging.getLogger(__name__)

# Stale run cleanup: runs older than this are evicted
DEFAULT_MAX_AGE = 3600  # 1 hour
# Maximum runs to keep in memory
MAX_REGISTRY_SIZE = 200


class SubagentRegistry:
    """In-memory registry with optional async-safe JSON disk persistence."""

    def __init__(self, persist_path: Path | str | None = None):
        self._runs: dict[str, SubagentRun] = {}
        self._by_user: dict[str, list[str]] = {}  # user_id -> [run_ids]
        self._persist_path = Path(persist_path) if persist_path else None
        self._lock = asyncio.Lock()

    # ── Mutating API (async, lock-guarded) ───────────────────

    async def register(self, run: SubagentRun) -> None:
        """Register a new run. Concurrent-safe."""
        async with self._lock:
            self._runs[run.run_id] = run
            user_runs = self._by_user.setdefault(run.parent_user_id, [])
            user_runs.append(run.run_id)
            await self._persist_locked()

    async def update(self, run_id: str, **kwargs: Any) -> SubagentRun | None:
        """Update fields on an existing run. Returns updated run or None."""
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            for key, value in kwargs.items():
                if hasattr(run, key):
                    setattr(run, key, value)
            await self._persist_locked()
            return run

    async def cleanup_stale(self, max_age: float = DEFAULT_MAX_AGE) -> int:
        """Remove terminal runs older than max_age. Returns count removed.

        Concurrent-safe — serialized via `self._lock`.
        """
        async with self._lock:
            removed = self._cleanup_stale_locked(max_age)
            if removed:
                await self._persist_locked()
            return removed

    async def persist(self) -> None:
        """Force persist to disk (public API)."""
        async with self._lock:
            await self._persist_locked()

    # ── Read API (sync, snapshot-safe) ───────────────────────

    def get(self, run_id: str) -> SubagentRun | None:
        return self._runs.get(run_id)

    def get_active_for_user(self, user_id: str) -> list[SubagentRun]:
        """Get all non-terminal runs for a user."""
        run_ids = list(self._by_user.get(user_id, []))
        return [
            self._runs[rid]
            for rid in run_ids
            if rid in self._runs and not self._runs[rid].is_terminal
        ]

    def get_recent_for_user(self, user_id: str, limit: int = 10) -> list[SubagentRun]:
        """Get most recent runs for a user (any status)."""
        run_ids = list(self._by_user.get(user_id, []))
        runs = [self._runs[rid] for rid in run_ids if rid in self._runs]
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    def count_active_for_user(self, user_id: str) -> int:
        return len(self.get_active_for_user(user_id))

    def restore(self) -> None:
        """Restore registry from disk on startup.

        Sync because it runs once at startup before the event loop is
        servicing hot-path traffic.
        """
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

    # ── Internals ────────────────────────────────────────────

    def _cleanup_stale_locked(self, max_age: float) -> int:
        """Evict terminal runs older than max_age. Caller MUST hold the lock."""
        now = time.time()
        to_remove: list[str] = [
            run_id
            for run_id, run in list(self._runs.items())
            if run.is_terminal and (now - run.created_at) > max_age
        ]
        for run_id in to_remove:
            run = self._runs.pop(run_id, None)
            if run:
                user_runs = self._by_user.get(run.parent_user_id, [])
                if run_id in user_runs:
                    user_runs.remove(run_id)
        if to_remove:
            logger.debug("Cleaned up %d stale runs", len(to_remove))
        return len(to_remove)

    async def _persist_locked(self) -> None:
        """Snapshot under the lock, then write off-loop. Caller holds the lock."""
        if not self._persist_path:
            return

        # Enforce size limit while still under the lock.
        if len(self._runs) > MAX_REGISTRY_SIZE:
            self._cleanup_stale_locked(max_age=1800)  # 30 min aggressive cleanup

        # Build a serializable snapshot under the lock so the worker thread
        # never touches `self._runs` (which the event loop may mutate again
        # as soon as we release the lock).
        snapshot = [r.to_dict() for r in self._runs.values()]
        path = self._persist_path
        await asyncio.to_thread(self._persist_sync, snapshot, path)

    @staticmethod
    def _persist_sync(snapshot: list[dict[str, Any]], path: Path) -> None:
        """Write snapshot to disk atomically. Runs on a worker thread."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"runs": snapshot}, default=str))
            tmp.replace(path)
        except Exception as e:
            logger.warning("Failed to persist registry: %s", e)
