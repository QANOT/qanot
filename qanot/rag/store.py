"""Vector store implementations for RAG."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import struct
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A single search result from the vector store."""

    chunk_id: str
    text: str
    metadata: dict
    score: float  # 0..1, higher is better


class VectorStore(ABC):
    """Abstract base for vector stores."""

    @abstractmethod
    def add(
        self,
        texts: list[str],
        embeddings: list[list[float]],
        *,
        source: str = "",
        user_id: str = "",
        metadatas: list[dict] | None = None,
    ) -> list[str]:
        """Add chunks with embeddings. Returns list of chunk IDs."""
        ...

    @abstractmethod
    def search(
        self,
        query_embedding: list[float],
        *,
        top_k: int = 5,
        user_id: str | None = None,
        source: str | None = None,
    ) -> list[SearchResult]:
        """Search for similar chunks."""
        ...

    @abstractmethod
    def delete_source(self, source: str) -> int:
        """Delete all chunks from a source. Returns count deleted."""
        ...

    @abstractmethod
    def list_sources(self) -> list[dict]:
        """List distinct sources with chunk counts."""
        ...

    async def async_add(
        self,
        texts: list[str],
        embeddings: list[list[float]],
        **kwargs,
    ) -> list[str]:
        """Async wrapper for add()."""
        return await asyncio.to_thread(self.add, texts, embeddings, **kwargs)

    async def async_search(
        self,
        query_embedding: list[float],
        **kwargs,
    ) -> list[SearchResult]:
        """Async wrapper for search()."""
        return await asyncio.to_thread(self.search, query_embedding, **kwargs)


def _serialize_f32(vec: list[float]) -> bytes:
    """Serialize a float32 vector to bytes for sqlite-vec."""
    return struct.pack(f"<{len(vec)}f", *vec)


class SqliteVecStore(VectorStore):
    """SQLite-backed vector store using sqlite-vec extension."""

    def __init__(self, db_path: str, dimensions: int = 768):
        self.db_path = db_path
        self.dimensions = dimensions
        self._conn: sqlite3.Connection | None = None
        self._vec_available = False
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema and load sqlite-vec."""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

        # Load sqlite-vec extension
        try:
            import sqlite_vec

            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            self._vec_available = True
            logger.info("sqlite-vec extension loaded successfully")
        except (ImportError, OSError) as exc:
            logger.warning(
                "sqlite-vec not available (%s). "
                "Vector search will be disabled. "
                "Install with: pip install sqlite-vec",
                exc,
            )

        # Create chunks metadata table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            )
        """)

        # Create virtual table for vector search
        if self._vec_available:
            self._conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec
                USING vec0(embedding float[{self.dimensions}])
            """)

        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_user_id ON chunks(user_id)"
        )
        self._conn.commit()

    def add(
        self,
        texts: list[str],
        embeddings: list[list[float]],
        *,
        source: str = "",
        user_id: str = "",
        metadatas: list[dict] | None = None,
    ) -> list[str]:
        """Add text chunks with their embeddings to the store."""
        if len(texts) != len(embeddings):
            raise ValueError(
                f"texts ({len(texts)}) and embeddings ({len(embeddings)}) must have same length"
            )

        if metadatas and len(metadatas) != len(texts):
            raise ValueError(
                f"metadatas ({len(metadatas)}) must match texts ({len(texts)})"
            )

        conn = self._conn
        assert conn is not None, "Database not initialized"

        now = time.time()
        chunk_ids: list[str] = []

        for i, (text, embedding) in enumerate(zip(texts, embeddings)):
            chunk_id = uuid.uuid4().hex[:16]
            meta = metadatas[i] if metadatas else {}
            meta_json = json.dumps(meta, ensure_ascii=False)

            cursor = conn.execute(
                "INSERT INTO chunks (id, text, source, user_id, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chunk_id, text, source, user_id, meta_json, now),
            )

            if self._vec_available:
                # Use chunks table rowid to keep both tables in sync
                conn.execute(
                    "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                    (cursor.lastrowid, _serialize_f32(embedding)),
                )

            chunk_ids.append(chunk_id)

        conn.commit()
        logger.debug("Added %d chunks from source=%r", len(chunk_ids), source)
        return chunk_ids

    def search(
        self,
        query_embedding: list[float],
        *,
        top_k: int = 5,
        user_id: str | None = None,
        source: str | None = None,
    ) -> list[SearchResult]:
        """Search for chunks similar to the query embedding."""
        if not self._vec_available:
            logger.warning("Vector search unavailable — sqlite-vec not loaded")
            return []

        conn = self._conn
        assert conn is not None, "Database not initialized"

        # Fetch more candidates to allow for metadata filtering
        fetch_limit = top_k * 4 if (user_id or source) else top_k

        rows = conn.execute(
            "SELECT rowid, distance FROM chunks_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (_serialize_f32(query_embedding), fetch_limit),
        ).fetchall()

        if not rows:
            return []

        # Map rowids to chunk metadata
        rowid_distance = {row[0]: row[1] for row in rows}
        rowid_list = list(rowid_distance.keys())

        # Retrieve chunks in order of insertion (rowid corresponds to insertion order)
        # We need to map vec rowids back to chunk ids
        # Since we insert into chunks and chunks_vec in lockstep, we use ROWID ordering
        placeholders = ",".join("?" for _ in rowid_list)
        chunk_rows = conn.execute(
            f"SELECT rowid, id, text, source, user_id, metadata FROM chunks "
            f"WHERE rowid IN ({placeholders})",
            rowid_list,
        ).fetchall()

        results: list[SearchResult] = []
        for crow in chunk_rows:
            c_rowid = crow[0]
            c_id = crow[1]
            c_text = crow[2]
            c_source = crow[3]
            c_user_id = crow[4]
            c_metadata = json.loads(crow[5])

            # Apply metadata filters
            if user_id and c_user_id != user_id:
                continue
            if source and c_source != source:
                continue

            distance = rowid_distance.get(c_rowid, 1.0)
            score = 1.0 / (1.0 + distance)

            results.append(
                SearchResult(
                    chunk_id=c_id,
                    text=c_text,
                    metadata={**c_metadata, "source": c_source, "user_id": c_user_id},
                    score=score,
                )
            )

        # Sort by score descending, limit to top_k
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def delete_source(self, source: str) -> int:
        """Delete all chunks from a given source."""
        conn = self._conn
        assert conn is not None, "Database not initialized"

        # Get rowids to delete from vec table
        if self._vec_available:
            rowids = conn.execute(
                "SELECT rowid FROM chunks WHERE source = ?", (source,)
            ).fetchall()
            for (rowid,) in rowids:
                conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (rowid,))

        cursor = conn.execute("DELETE FROM chunks WHERE source = ?", (source,))
        count = cursor.rowcount
        conn.commit()
        logger.debug("Deleted %d chunks from source=%r", count, source)
        return count

    def list_sources(self) -> list[dict]:
        """List distinct sources with chunk counts."""
        conn = self._conn
        assert conn is not None, "Database not initialized"

        rows = conn.execute(
            "SELECT source, COUNT(*) as chunk_count, MIN(created_at) as first_added "
            "FROM chunks GROUP BY source ORDER BY source"
        ).fetchall()

        return [
            {
                "source": row[0],
                "chunk_count": row[1],
                "first_added": row[2],
            }
            for row in rows
        ]

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
