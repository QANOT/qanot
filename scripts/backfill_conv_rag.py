"""One-shot backfill: index historical conversation turns into RAG.

Live agent loop (qanot/agent/loop.py:_handle_end_turn) indexes every new
turn-pair as it happens — source ``conv:{conv_key}:{ISO timestamp}``.
This script walks the existing JSONL session files and back-fills the
same data for everything that pre-dates the live hook.

After running once, the bot can semantically retrieve "what we discussed
two weeks ago about X" — not just the hand-curated MEMORY.md summary.

Usage inside the running container:

    docker exec qanot-bot-1545224574 python3 -m scripts.backfill_conv_rag \\
        --sessions /data/sessions \\
        --workspace /data/workspace

Re-runnable: existing chunks with the same source path get skipped
(the RAG indexer's source-key check is the dedupe boundary). Long
sessions take a couple of minutes per 1000 turns on FastEmbed CPU.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Bounds matched to the live hook in agent/loop.py so retrieved chunks
# look identical regardless of source (live vs back-filled).
USER_TEXT_MAX_CHARS = 600
ASSISTANT_TEXT_MAX_CHARS = 1500

# Skip messages with content shorter than this — usually empty
# tool-use scaffolding, not useful for retrieval.
MIN_TEXT_CHARS = 5


# ────────── JSONL parsing ──────────


def _extract_text_blocks(content: Any) -> str:
    """Pull the text from a list of content blocks (Anthropic-style)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text") or ""
            if text:
                out.append(text)
    return "\n".join(out)


def _entry_text(entry: dict) -> tuple[str | None, str, str]:
    """Return ``(role, conv_key, text)`` for an entry, or ``(None, "", "")``.

    Only ``user`` and ``assistant`` roles produce non-None roles. Empty
    text or tool-use-only blocks are filtered out — the resulting chunk
    would just be noise.
    """
    msg = entry.get("message") or {}
    role = msg.get("role") or ""
    conv_key = str(entry.get("user_id") or "").strip()
    if role not in ("user", "assistant") or not conv_key:
        return None, "", ""
    text = _extract_text_blocks(msg.get("content"))
    text = text.strip()
    if len(text) < MIN_TEXT_CHARS:
        return None, "", ""
    return role, conv_key, text


def _iter_session_files(sessions_dir: Path):
    """Yield session JSONL paths sorted oldest → newest. Older first
    so the RAG store ends up with the most recent versions on top of
    any duplicate source keys (last write wins, but we dedupe by
    source so this is just defensive)."""
    files = sorted(sessions_dir.glob("*.jsonl"))
    for f in files:
        yield f


def _pair_turns(entries):
    """Generator yielding ``(conv_key, user_text, assistant_text, ts)``
    tuples for each user→assistant pair within the same conv_key.

    The session JSONL interleaves turns from many conv_keys (per-thread,
    per-group). We keep a pending user-text per conv_key and pair it
    with the next assistant from the same conv_key. Subsequent user
    messages without an intervening assistant overwrite the pending
    one (the user must have given up on the in-flight turn).
    """
    pending_user: dict[str, tuple[str, str]] = {}
    for entry in entries:
        role, conv_key, text = _entry_text(entry)
        if role is None:
            continue
        ts = entry.get("timestamp") or ""
        if role == "user":
            pending_user[conv_key] = (text, ts)
        elif role == "assistant":
            prior = pending_user.pop(conv_key, None)
            if prior is None:
                # Orphan assistant message — happens if a bot proactive
                # message fires (no preceding user). Index the assistant
                # text alone so it's still retrievable.
                yield (conv_key, "", text, ts)
            else:
                user_text, _user_ts = prior
                yield (conv_key, user_text, text, ts)


# ────────── Main backfill ──────────


async def run_backfill(
    *, sessions_dir: Path, workspace_dir: Path, dry_run: bool = False,
) -> dict[str, int]:
    """Walk session files and ingest each turn-pair via the existing
    RAG indexer. Returns ``{"pairs_seen": N, "pairs_indexed": N}``.
    """
    from qanot.config import load_config
    from qanot.rag import (
        create_embedder, MemoryIndexer, RAGEngine, SqliteVecStore,
    )

    # The indexer needs the same embedder + store the live bot uses,
    # so the chunks land in the same database. Reuse ``load_config``
    # so we honour the bot's rag.db path + embedder choice.
    cfg = load_config(str(workspace_dir.parent / "config.json"))
    embedder = create_embedder(cfg)
    if embedder is None:
        raise RuntimeError(
            "No embedder available — check rag_enabled + embedder config",
        )
    store = SqliteVecStore(
        db_path=str(workspace_dir / "rag.db"),
        dimensions=embedder.dimensions,
    )
    engine = RAGEngine(embedder=embedder, store=store)
    indexer = MemoryIndexer(engine, str(workspace_dir))

    stats = defaultdict(int)
    seen_sources: set[str] = set()

    for path in _iter_session_files(sessions_dir):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            logger.warning("skipping %s: %s", path.name, e)
            continue

        entries: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") != "message":
                continue
            entries.append(e)

        if not entries:
            continue

        logger.info("%s: %d entries", path.name, len(entries))

        # Group entries by date+hour bucket so the source key has the
        # same granularity as live indexing. The live hook uses
        # ``%Y-%m-%dT%H:%M`` precision; we match.
        for conv_key, user_text, assistant_text, ts in _pair_turns(entries):
            stats["pairs_seen"] += 1
            if not assistant_text:
                continue
            user_trunc = user_text[:USER_TEXT_MAX_CHARS] if user_text else ""
            assistant_trunc = assistant_text[:ASSISTANT_TEXT_MAX_CHARS]
            text_for_index = (
                f"User: {user_trunc}\n\nAgent: {assistant_trunc}"
                if user_trunc
                else f"Agent: {assistant_trunc}"
            )
            # Source key — minute-precision matches live hook. The
            # in-memory ``seen_sources`` set dedupes within this run;
            # MemoryIndexer's own hash-based skip handles cross-run.
            ts_str = _normalise_ts(ts)
            source = f"conv:{conv_key}:{ts_str}"
            if source in seen_sources:
                continue
            seen_sources.add(source)

            if dry_run:
                stats["pairs_indexed"] += 1
                continue

            try:
                await indexer.index_text(
                    text_for_index,
                    source=source,
                    user_id=conv_key,
                    metadata={
                        "kind": "conversation",
                        "conv_key": conv_key,
                        "date": ts_str,
                        "backfilled": True,
                    },
                )
                stats["pairs_indexed"] += 1
            except Exception as e:
                logger.warning(
                    "index_text failed for %s: %s", source, e,
                )
                stats["errors"] += 1

            # Progress beat so the operator sees something on big
            # archives.
            if stats["pairs_indexed"] % 100 == 0:
                logger.info(
                    "progress: %d pairs indexed", stats["pairs_indexed"],
                )

    return dict(stats)


def _normalise_ts(ts: str) -> str:
    """Truncate ISO timestamp to minute precision; fall back to ``now``."""
    if not ts:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    try:
        # Drop microseconds + tz offset to match live hook format.
        return ts[:16]  # "2026-05-13T11:31"
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessions", default="/data/sessions",
        help="Directory containing daily JSONL session files",
    )
    parser.add_argument(
        "--workspace", default="/data/workspace",
        help="Workspace directory (where rag.db lives)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Walk files + count pairs without writing to RAG",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    sessions_dir = Path(args.sessions)
    workspace_dir = Path(args.workspace)
    if not sessions_dir.is_dir():
        raise SystemExit(f"sessions dir not found: {sessions_dir}")
    if not workspace_dir.is_dir():
        raise SystemExit(f"workspace dir not found: {workspace_dir}")

    stats = asyncio.run(run_backfill(
        sessions_dir=sessions_dir,
        workspace_dir=workspace_dir,
        dry_run=args.dry_run,
    ))

    logger.info("=" * 60)
    logger.info("Backfill complete:")
    for k, v in stats.items():
        logger.info("  %s: %d", k, v)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
