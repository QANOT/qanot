"""Atomic memo store — file-per-memo writes, list/load by scope.

The store layer sits on top of ``qanot/memos/spec.py``. It exposes the
write operations the WAL Protocol and the migration script need, plus
the read operations the router consumes per turn.

Storage layout:
    <workspace>/memories/
        <slug>.md                       # MemoSpec files
        .index.json                     # Optional cache of (name, description,
                                        #   type, user_scope, thread_scope)
        .archive/<slug>.md              # Soft-deleted memos
        learnings/                      # legacy — left alone (memory_tool.py)
        daily-notes/                    # legacy — left alone

The store is intentionally co-located with ``qanot/tools/memory_tool.py``
(the Anthropic ``memory_20250818`` protocol) so the agent's own
``memory view /memories`` calls show memos in the same listing. No new
directory, no parallel hierarchy.

Atomic writes via tempfile + os.replace; same pattern as
``qanot/skills/store.py``. No file locking — concurrent writers from
multiple workers race at last-writer-wins granularity, which is
acceptable for the WAL path (each memo is single-author by intent).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .spec import (
    MemoSpec,
    MemoSpecError,
    MemoType,
    parse_memo_file,
    render_memo,
)

logger = logging.getLogger(__name__)


MEMOS_DIR_NAME = "memories"
ARCHIVE_DIR_NAME = ".archive"

# Subdirectories the existing memory_tool.py uses for its own purposes.
# We skip them during memo discovery so a daily-note transcript doesn't
# get parsed as a malformed memo.
EXCLUDED_SUBDIRS = frozenset({"daily-notes", "learnings", ARCHIVE_DIR_NAME})


class StoreError(Exception):
    """Raised on memo store operation failures."""


@dataclass
class WriteResult:
    """Returned by every state-changing operation for logging / audit."""

    name: str
    path: Path
    action: str  # "created" | "updated" | "archived" | "unarchived" | "deleted"


# ─── helpers ─────────────────────────────────────────────────────


def _atomic_write_text(path: Path, content: str) -> None:
    """tempfile-in-same-dir + os.replace. No partial files on crash."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─── MemoStore ───────────────────────────────────────────────────


class MemoStore:
    """Read/write memos under ``<workspace>/memories/``.

    Caller passes the workspace directory; the store appends ``memories/``
    so the layout always matches ``qanot/tools/memory_tool.py``.
    """

    def __init__(self, workspace_dir: str | Path):
        self.workspace = Path(workspace_dir)
        self.root = self.workspace / MEMOS_DIR_NAME

    @property
    def archive_dir(self) -> Path:
        return self.root / ARCHIVE_DIR_NAME

    # ─── read ───────────────────────────────────────────────────

    def list_all(self) -> list[MemoSpec]:
        """Load every memo file under the store, sorted by name.

        Skips ``EXCLUDED_SUBDIRS`` (daily-notes, learnings, .archive) and
        files without the canonical frontmatter — these are treated as
        legacy artefacts the memory_tool.py still writes. We log at
        DEBUG, not WARNING, because the workspace will normally contain
        more files than memos for a long time.
        """
        if not self.root.is_dir():
            return []
        out: list[MemoSpec] = []
        for entry in sorted(self.root.iterdir()):
            if entry.is_dir():
                continue  # skip every subdir (legacy daily-notes, learnings, .archive)
            if entry.suffix.lower() != ".md":
                continue
            if entry.name.startswith("."):
                continue  # .index.json and other dotfiles
            try:
                spec = parse_memo_file(entry)
            except MemoSpecError as exc:
                logger.debug("skipping non-memo file %s: %s", entry.name, exc)
                continue
            out.append(spec)
        return out

    def list_in_scope(
        self,
        *,
        user_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[MemoSpec]:
        """Pre-filter pass — drop out-of-scope memos before the router runs.

        This is the cheap path: scope filtering is a simple equality check,
        no LLM / embedding cost. The router consumes the output of this
        method, so any memo that didn't match here is invisible to it.
        """
        return [
            m for m in self.list_all()
            if m.matches_scope(user_id=user_id, thread_id=thread_id)
        ]

    def load(self, name: str) -> MemoSpec | None:
        """Load a single memo by name. Returns None if missing."""
        path = self.root / f"{name}.md"
        if not path.is_file():
            return None
        try:
            return parse_memo_file(path)
        except MemoSpecError as exc:
            logger.warning("memo %s is malformed: %s", name, exc)
            return None

    # ─── write ──────────────────────────────────────────────────

    def upsert(
        self,
        name: str,
        description: str,
        memo_type: MemoType | str,
        body: str,
        *,
        user_scope: str = "",
        thread_scope: str = "",
        why: str = "",
        how_to_apply: str = "",
    ) -> WriteResult:
        """Create or overwrite a memo. Atomic.

        The WAL writer calls this with each captured rule. We deliberately
        use upsert semantics (not strict create) so re-stating the same
        rule refines it rather than collecting duplicate files. The
        router will keep using the latest description for relevance
        scoring; old versions are not retained — the file's git history
        (or, in workspace terms, the periodic backup) is the audit log.
        """
        # Render via the spec module so validation lives in one place.
        content = render_memo(
            name=name, description=description, memo_type=memo_type, body=body,
            user_scope=user_scope, thread_scope=thread_scope,
            why=why, how_to_apply=how_to_apply,
        )
        path = self.root / f"{name}.md"
        existed = path.is_file()
        _atomic_write_text(path, content)

        # Re-parse to guarantee on-disk validity. If validation now fails,
        # we have a write-but-don't-load bug that needs a loud failure.
        try:
            parse_memo_file(path)
        except MemoSpecError as exc:
            raise StoreError(f"post-write validation failed: {exc}") from exc

        return WriteResult(
            name=name, path=path,
            action="updated" if existed else "created",
        )

    def archive(self, name: str) -> WriteResult:
        """Move the memo to ``.archive/``. Recoverable via ``unarchive``."""
        src = self.root / f"{name}.md"
        if not src.is_file():
            raise StoreError(f"memo {name!r} not found at {src}")
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        dst = self.archive_dir / f"{name}.md"
        if dst.exists():
            raise StoreError(f"archive slot already occupied: {dst}")
        shutil.move(str(src), str(dst))
        return WriteResult(name=name, path=dst, action="archived")

    def unarchive(self, name: str) -> WriteResult:
        src = self.archive_dir / f"{name}.md"
        if not src.is_file():
            raise StoreError(f"no archived memo named {name!r}")
        dst = self.root / f"{name}.md"
        if dst.exists():
            raise StoreError(f"cannot un-archive — slot occupied: {dst}")
        shutil.move(str(src), str(dst))
        return WriteResult(name=name, path=dst, action="unarchived")

    def delete(self, name: str) -> WriteResult:
        """Hard delete (skips archive). Use only for migration / cleanup."""
        path = self.root / f"{name}.md"
        if not path.is_file():
            raise StoreError(f"memo {name!r} not found")
        path.unlink()
        return WriteResult(name=name, path=path, action="deleted")

    # ─── bulk helpers ───────────────────────────────────────────

    def write_many(self, memos: Iterable[dict]) -> list[WriteResult]:
        """Convenience for the migration script — write a batch of memos.

        Each dict must carry ``name``, ``description``, ``memo_type``,
        ``body``; optional fields match :py:meth:`upsert`.
        """
        results: list[WriteResult] = []
        for memo in memos:
            results.append(self.upsert(**memo))
        return results
