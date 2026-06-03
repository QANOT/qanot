"""Full-text search over past session transcripts (JSONL) via SQLite FTS5.

Qanot's RAG only indexes memory *markdown* (MEMORY.md, daily notes); the raw
session JSONL transcripts had no search at all — so "what did we decide about
X last month?" was unanswerable mid-chat. This module builds an incremental
FTS5 index over ``{sessions_dir}/YYYY-MM-DD.jsonl`` and exposes a
``session_search`` tool that returns ranked, session-grouped snippets.

Indexing is incremental by file mtime: past-day files are immutable and
indexed once; only today's file is re-indexed as it grows.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def _entry_text(entry: dict) -> tuple[str, str, str, str]:
    """Extract (role, text, ts, user_id) from a session JSONL entry."""
    msg = entry.get("message") or {}
    role = msg.get("role", "")
    ts = entry.get("timestamp", "")
    user_id = str(entry.get("user_id", "") or "")
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                parts.append(block["text"])
            elif block.get("type") == "tool_use" and block.get("name"):
                parts.append(f"[tool: {block['name']}]")
        text = "\n".join(parts)
    else:
        text = ""
    return role, text, ts, user_id


def _to_fts_query(raw: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH (AND of quoted terms)."""
    words = _WORD_RE.findall(raw or "")
    if not words:
        return ""
    # Quote each term so FTS5 operators in user text can't break the query.
    return " ".join(f'"{w}"' for w in words[:16])


class SessionSearchIndex:
    """Incremental FTS5 index over session JSONL files."""

    def __init__(self, sessions_dir: str, db_path: str):
        self.sessions_dir = Path(sessions_dir)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._available = True
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5("
                    "text, session_date UNINDEXED, role UNINDEXED, "
                    "ts UNINDEXED, user_id UNINDEXED, tokenize='unicode61')"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS indexed_files "
                    "(path TEXT PRIMARY KEY, mtime REAL)"
                )
        except sqlite3.OperationalError as e:
            logger.warning("FTS5 unavailable — session search disabled: %s", e)
            self._available = False

    def sync(self) -> int:
        """Index any new/changed session files. Returns # files (re)indexed."""
        if not self._available or not self.sessions_dir.exists():
            return 0
        reindexed = 0
        with self._connect() as conn:
            known = {
                r["path"]: r["mtime"]
                for r in conn.execute("SELECT path, mtime FROM indexed_files")
            }
            for f in sorted(self.sessions_dir.glob("*.jsonl")):
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                if abs(known.get(str(f), -1.0) - mtime) < 1e-6:
                    continue
                self._index_file(conn, f, mtime)
                reindexed += 1
            conn.commit()
        return reindexed

    def _index_file(self, conn: sqlite3.Connection, f: Path, mtime: float) -> None:
        session_date = f.stem
        # Drop any prior rows for this date (re-index in full — cheap per day).
        rowids = [
            r["rowid"]
            for r in conn.execute(
                "SELECT rowid FROM entries WHERE session_date = ?", (session_date,)
            )
        ]
        if rowids:
            conn.executemany(
                "DELETE FROM entries WHERE rowid = ?", [(rid,) for rid in rowids]
            )
        batch: list[tuple] = []
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role, text, ts, user_id = _entry_text(entry)
                if text and text.strip():
                    batch.append((text, session_date, role, ts, user_id))
        except OSError as e:
            logger.debug("session file read failed (%s): %s", f, e)
            return
        if batch:
            conn.executemany(
                "INSERT INTO entries(text, session_date, role, ts, user_id) "
                "VALUES(?,?,?,?,?)",
                batch,
            )
        conn.execute(
            "INSERT OR REPLACE INTO indexed_files(path, mtime) VALUES(?,?)",
            (str(f), mtime),
        )

    def search(
        self, query: str, *, limit: int = 5, user_id: str | None = None,
        days: int | None = None, per_session: int = 4,
    ) -> dict[str, Any]:
        """Search transcripts, return matches grouped by session (most recent first)."""
        if not self._available:
            return {"error": "Session search unavailable (FTS5 not compiled in)."}
        self.sync()
        fts = _to_fts_query(query)
        if not fts:
            return {"error": "Empty query."}
        clauses = ["entries MATCH ?"]
        args: list[Any] = [fts]
        if user_id:
            clauses.append("user_id = ?")
            args.append(str(user_id))
        if days and days > 0:
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            clauses.append("session_date >= ?")
            args.append(cutoff)
        sql = (
            "SELECT session_date, role, ts, user_id, "
            "snippet(entries, 0, '«', '»', '…', 12) AS snip, "
            "bm25(entries) AS rank "
            "FROM entries WHERE " + " AND ".join(clauses) +
            " ORDER BY rank LIMIT ?"
        )
        args.append(max(limit, 1) * max(per_session, 1) * 3)
        try:
            with self._connect() as conn:
                rows = [dict(r) for r in conn.execute(sql, args)]
        except sqlite3.OperationalError as e:
            return {"error": f"search failed: {e}"}

        # Group by session date, newest first, capping snippets per session.
        sessions: dict[str, list[dict]] = {}
        for r in rows:
            d = r["session_date"]
            bucket = sessions.setdefault(d, [])
            if len(bucket) < per_session:
                bucket.append({
                    "role": r["role"],
                    "ts": r["ts"],
                    "snippet": (r["snip"] or "").replace("\n", " ").strip()[:300],
                })
        ordered = sorted(sessions.items(), key=lambda kv: kv[0], reverse=True)[:limit]
        return {
            "query": query,
            "total_matches": len(rows),
            "sessions": [
                {"date": d, "matches": m} for d, m in ordered
            ],
            "note": "" if ordered else "Hech narsa topilmadi.",
        }


def register_session_search_tool(
    registry, sessions_dir: str, workspace_dir: str,
    get_user_id=None,
) -> None:
    """Register the ``session_search`` tool backed by a persistent FTS5 index."""
    index = SessionSearchIndex(
        sessions_dir=sessions_dir,
        db_path=str(Path(workspace_dir) / "session_search.db"),
    )

    async def session_search(params: dict) -> str:
        import asyncio
        query = (params.get("query") or "").strip()
        if not query:
            return json.dumps({"error": "query is required"})
        limit = int(params.get("limit", 5) or 5)
        days = params.get("days")
        days = int(days) if days else None
        scope_self = params.get("only_me", False)
        uid = (get_user_id() if (scope_self and get_user_id) else None)
        # FTS + file I/O off the event loop.
        result = await asyncio.to_thread(
            index.search, query, limit=limit, user_id=uid, days=days,
        )
        return json.dumps(result, ensure_ascii=False, default=str)

    registry.register(
        name="session_search",
        description=(
            "Search your OWN past conversation transcripts (all sessions, not "
            "just recent history) by keyword. Use to recall what was discussed "
            "or decided days/weeks ago — 'o'tgan oy X haqida nima gaplashgandik?'. "
            "Returns ranked snippets grouped by date. Complements memory_search "
            "(which only covers saved memory files)."
        ),
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Keywords to search for."},
                "limit": {"type": "number", "description": "Max sessions to return (default 5)."},
                "days": {"type": "number", "description": "Only search the last N days (optional)."},
                "only_me": {"type": "boolean", "description": "Limit to the current user's messages (default false)."},
            },
        },
        handler=session_search,
    )
