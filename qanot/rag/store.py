"""Vector store with FTS5 full-text search and embedding cache."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import struct
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Default max cached embeddings before LRU eviction
DEFAULT_CACHE_MAX_ENTRIES = 10_000


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
    def search_fts(self, query: str, *, top_k: int = 5) -> list[SearchResult]:
        """Full-text search using FTS5."""
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

    async def async_search_fts(self, query: str, **kwargs) -> list[SearchResult]:
        """Async wrapper for search_fts()."""
        return await asyncio.to_thread(self.search_fts, query, **kwargs)


def _serialize_f32(vec: list[float]) -> bytes:
    """Serialize a float32 vector to bytes for sqlite-vec."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _hash_text(text: str) -> str:
    """Hash text content for embedding cache key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _build_fts_query(raw: str) -> str | None:
    """Build FTS5 query from raw text. Returns None if no valid tokens."""
    import re
    tokens = re.findall(r"[\w]+", raw, re.UNICODE)
    if not tokens:
        return None
    # Quote each token and AND them together
    quoted = [f'"{t}"' for t in tokens if len(t) > 1]
    if not quoted:
        return None
    return " AND ".join(quoted)


def _bm25_rank_to_score(rank: float) -> float:
    """Convert SQLite FTS5 bm25() rank to 0..1 score.

    FTS5 bm25() returns negative values where more negative = more relevant.
    """
    if not isinstance(rank, (int, float)):
        return 0.01
    if rank < 0:
        relevance = -rank
        return relevance / (1.0 + relevance)
    return 1.0 / (1.0 + rank)


class SqliteVecStore(VectorStore):
    """SQLite-backed vector store with FTS5 and embedding cache."""

    def __init__(
        self,
        db_path: str,
        dimensions: int = 768,
        cache_max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
    ):
        self.db_path = db_path
        self.dimensions = dimensions
        self.cache_max_entries = cache_max_entries
        self._conn: sqlite3.Connection | None = None
        self._vec_available = False
        self._fts_available = False
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema: chunks, vec, FTS5, embedding cache."""
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

        # FTS5 full-text search index
        try:
            self._conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    text,
                    id UNINDEXED,
                    source UNINDEXED
                )
            """)
            self._fts_available = True
            logger.info("FTS5 full-text search index available")
        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 not available (%s). Keyword search will use in-memory BM25.", exc)

        # Embedding cache table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_cache (
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                hash TEXT NOT NULL,
                embedding BLOB NOT NULL,
                dims INTEGER NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (provider, model, hash)
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_updated ON embedding_cache(updated_at)"
        )

        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_user_id ON chunks(user_id)"
        )
        self._conn.commit()

    # ── Embedding cache ──

    def cache_get(
        self, hashes: list[str], provider: str, model: str
    ) -> dict[str, list[float]]:
        """Batch lookup embedding cache. Returns {hash: embedding} for hits."""
        if not hashes:
            return {}

        conn = self._conn
        assert conn is not None

        result: dict[str, list[float]] = {}
        # Query in batches of 400 (SQLite variable limit)
        for i in range(0, len(hashes), 400):
            batch = hashes[i : i + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT hash, embedding, dims FROM embedding_cache "
                f"WHERE provider = ? AND model = ? AND hash IN ({placeholders})",
                [provider, model, *batch],
            ).fetchall()
            for row in rows:
                h = row[0]
                blob = row[1]
                dims = row[2]
                vec = list(struct.unpack(f"<{dims}f", blob))
                result[h] = vec

        if result:
            logger.debug("Embedding cache: %d hits / %d queries", len(result), len(hashes))
        return result

    def cache_put(
        self,
        items: list[tuple[str, list[float]]],
        provider: str,
        model: str,
    ) -> None:
        """Batch insert/update embedding cache. items = [(hash, embedding), ...]."""
        if not items:
            return

        conn = self._conn
        assert conn is not None

        now = time.time()
        for h, embedding in items:
            dims = len(embedding)
            blob = struct.pack(f"<{dims}f", *embedding)
            conn.execute(
                "INSERT INTO embedding_cache (provider, model, hash, embedding, dims, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, model, hash) DO UPDATE SET "
                "embedding=excluded.embedding, dims=excluded.dims, updated_at=excluded.updated_at",
                (provider, model, h, blob, dims, now),
            )
        conn.commit()

        # LRU eviction
        self._prune_cache()

    def _prune_cache(self) -> None:
        """Evict oldest cache entries if over max_entries."""
        if self.cache_max_entries <= 0:
            return

        conn = self._conn
        assert conn is not None

        count = conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0]
        excess = count - self.cache_max_entries
        if excess > 0:
            conn.execute(
                "DELETE FROM embedding_cache WHERE rowid IN ("
                "  SELECT rowid FROM embedding_cache ORDER BY updated_at ASC LIMIT ?"
                ")",
                (excess,),
            )
            conn.commit()
            logger.debug("Cache pruned: evicted %d oldest entries", excess)

    # ── Chunk operations ──

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

        for i, emb in enumerate(embeddings):
            if len(emb) != self.dimensions:
                raise ValueError(
                    f"Embedding at index {i} has {len(emb)} dimensions, "
                    f"expected {self.dimensions}"
                )

        conn = self._conn
        assert conn is not None, "Database not initialized"

        now = time.time()
        chunk_ids: list[str] = []

        with self._lock:
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
                    conn.execute(
                        "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                        (cursor.lastrowid, _serialize_f32(embedding)),
                    )

                # FTS5 index
                if self._fts_available:
                    conn.execute(
                        "INSERT INTO chunks_fts (text, id, source) VALUES (?, ?, ?)",
                        (text, chunk_id, source),
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

        fetch_limit = top_k * 4 if (user_id or source) else top_k

        with self._lock:
            rows = conn.execute(
                "SELECT rowid, distance FROM chunks_vec "
                "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (_serialize_f32(query_embedding), fetch_limit),
            ).fetchall()

            if not rows:
                return []

            rowid_distance = {row[0]: row[1] for row in rows}
            rowid_list = list(rowid_distance.keys())

            placeholders = ",".join("?" for _ in rowid_list)
            chunk_rows = conn.execute(
                f"SELECT rowid, id, text, source, user_id, metadata, created_at FROM chunks "
                f"WHERE rowid IN ({placeholders})",
                rowid_list,
            ).fetchall()

        results: list[SearchResult] = []
        for crow in chunk_rows:
            c_rowid, c_id, c_text, c_source, c_user_id, c_metadata, c_created_at = (
                crow[0], crow[1], crow[2], crow[3], crow[4], json.loads(crow[5]), crow[6],
            )

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
                    metadata={
                        **c_metadata,
                        "source": c_source,
                        "user_id": c_user_id,
                        "created_at": c_created_at,
                    },
                    score=score,
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def search_fts(
        self,
        query: str,
        *,
        top_k: int = 5,
    ) -> list[SearchResult]:
        """Full-text search using FTS5 with BM25 ranking."""
        if not self._fts_available:
            return []

        conn = self._conn
        assert conn is not None

        fts_query = _build_fts_query(query)
        if not fts_query:
            return []

        try:
            with self._lock:
                rows = conn.execute(
                    "SELECT f.id, f.text, f.source, bm25(chunks_fts) as rank, c.created_at "
                    "FROM chunks_fts f LEFT JOIN chunks c ON f.id = c.id "
                    "WHERE chunks_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (fts_query, top_k),
                ).fetchall()
        except sqlite3.OperationalError as e:
            logger.debug("FTS5 query failed: %s", e)
            return []

        results: list[SearchResult] = []
        for row in rows:
            results.append(
                SearchResult(
                    chunk_id=row[0],
                    text=row[1],
                    metadata={"source": row[2], "created_at": row[4]},
                    score=_bm25_rank_to_score(row[3]),
                )
            )

        return results

    @property
    def fts_available(self) -> bool:
        return self._fts_available

    def delete_source(self, source: str) -> int:
        """Delete all chunks from a given source."""
        conn = self._conn
        assert conn is not None, "Database not initialized"

        with self._lock:
            if self._vec_available or self._fts_available:
                rows = conn.execute(
                    "SELECT rowid, id FROM chunks WHERE source = ?", (source,)
                ).fetchall()
                for rowid, cid in rows:
                    if self._vec_available:
                        conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (rowid,))
                    if self._fts_available:
                        conn.execute("DELETE FROM chunks_fts WHERE id = ?", (cid,))

            cursor = conn.execute("DELETE FROM chunks WHERE source = ?", (source,))
            count = cursor.rowcount
            conn.commit()
        logger.debug("Deleted %d chunks from source=%r", count, source)
        return count

    def list_sources(self) -> list[dict]:
        """List distinct sources with chunk counts."""
        conn = self._conn
        assert conn is not None, "Database not initialized"

        with self._lock:
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
