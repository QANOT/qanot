"""RAG engine that orchestrates embedding, storage, and retrieval."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from qanot.rag.chunker import BM25Index, chunk_text
from qanot.rag.embedder import Embedder
from qanot.rag.store import SearchResult, SqliteVecStore, VectorStore, _hash_text

logger = logging.getLogger(__name__)

# MMR similarity threshold — texts with >70% word overlap are considered redundant
MMR_SIMILARITY_THRESHOLD = 0.70


def _text_similarity(a: str, b: str) -> float:
    """Compute Jaccard word-overlap similarity between two texts."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _is_redundant(text: str, seen_texts: list[str]) -> bool:
    """Check if text is too similar to any already-selected result."""
    for seen in seen_texts:
        if _text_similarity(text, seen) > MMR_SIMILARITY_THRESHOLD:
            return True
    return False


@dataclass
class RAGResult:
    """Combined result from hybrid retrieval."""

    results: list[SearchResult]
    query: str
    sources_used: list[str] = field(default_factory=list)


class RAGEngine:
    """Orchestrates chunking, embedding, storage, and hybrid retrieval.

    Combines vector similarity search with keyword matching (FTS5 persistent
    or in-memory BM25 fallback) for robust hybrid retrieval.

    Uses embedding cache to avoid re-embedding unchanged content.
    """

    def __init__(
        self,
        embedder: Embedder | None,
        store: VectorStore,
        *,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        bm25_weight: float = 0.3,
    ):
        self.embedder = embedder
        self.store = store
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.bm25_weight = bm25_weight
        # In-memory BM25 as fallback when FTS5 is unavailable
        self._bm25 = BM25Index()
        self._use_fts5 = (
            isinstance(store, SqliteVecStore) and store.fts_available
        )

    @property
    def fts_mode(self) -> str:
        """Return which keyword search backend is active."""
        return "fts5" if self._use_fts5 else "bm25"

    @property
    def has_embedder(self) -> bool:
        """Whether vector embedding is available."""
        return self.embedder is not None

    async def _embed_with_cache(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        """Embed texts using cache when available.

        For SqliteVecStore: checks cache first, only embeds cache misses,
        then stores new embeddings in cache.
        For other stores or no embedder: embeds directly.
        """
        if self.embedder is None:
            raise RuntimeError("No embedder available — cannot generate embeddings")

        store = self.store
        # Use cache only with SqliteVecStore
        if not isinstance(store, SqliteVecStore):
            return await self.embedder.embed(texts)

        provider = self.embedder.provider_name
        model = getattr(self.embedder, "model", "default")

        # Hash all texts
        hashes = [_hash_text(t) for t in texts]

        # Batch cache lookup
        cached = store.cache_get(hashes, provider, model)

        # Find misses
        miss_indices: list[int] = []
        miss_texts: list[str] = []
        for i, h in enumerate(hashes):
            if h not in cached:
                miss_indices.append(i)
                miss_texts.append(texts[i])

        # Embed only misses
        if miss_texts:
            new_embeddings = await self.embedder.embed(miss_texts)
            # Store in cache
            cache_items = [
                (hashes[miss_indices[j]], new_embeddings[j])
                for j in range(len(miss_texts))
            ]
            store.cache_put(cache_items, provider, model)
            logger.debug(
                "Embedding cache: %d hits, %d misses (embedded)",
                len(texts) - len(miss_texts),
                len(miss_texts),
            )
        else:
            new_embeddings = []
            logger.debug("Embedding cache: all %d texts cached", len(texts))

        # Reassemble in original order
        result: list[list[float]] = []
        miss_idx = 0
        for i, h in enumerate(hashes):
            if h in cached:
                result.append(cached[h])
            else:
                result.append(new_embeddings[miss_idx])
                miss_idx += 1

        return result

    # Maximum allowed lengths for string inputs (defense-in-depth)
    _MAX_SOURCE_LEN = 1024
    _MAX_USER_ID_LEN = 256
    _MAX_TEXT_LEN = 10_000_000  # 10 MB
    _MAX_QUERY_LEN = 10_000

    @staticmethod
    def _validate_str(value: str, name: str, max_len: int) -> str:
        """Validate a string input: type check, strip, and enforce length."""
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string, got {type(value).__name__}")
        value = value.strip()
        if len(value) > max_len:
            raise ValueError(
                f"{name} exceeds maximum length ({len(value)} > {max_len})"
            )
        return value

    async def ingest(
        self,
        text: str,
        *,
        source: str = "",
        user_id: str = "",
        metadata: dict | None = None,
    ) -> list[str]:
        """Chunk, embed, and store a document.

        Uses embedding cache to skip re-embedding unchanged chunks.
        Falls back to FTS-only indexing if no embedder is available.
        """
        text = self._validate_str(text, "text", self._MAX_TEXT_LEN)
        source = self._validate_str(source, "source", self._MAX_SOURCE_LEN)
        user_id = self._validate_str(user_id, "user_id", self._MAX_USER_ID_LEN)
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError(f"metadata must be a dict or None, got {type(metadata).__name__}")
        chunks = chunk_text(
            text,
            max_tokens=self.chunk_size,
            overlap=self.chunk_overlap,
        )

        if not chunks:
            logger.debug("No chunks produced from source=%r", source)
            return []

        metadatas = [metadata or {} for _ in chunks]

        if self.embedder is not None:
            embeddings = await self._embed_with_cache(chunks)
            chunk_ids = await self.store.async_add(
                chunks,
                embeddings,
                source=source,
                user_id=user_id,
                metadatas=metadatas,
            )
        else:
            # FTS-only mode: store with zero embeddings matching store dimensions
            zero_embs = [[0.0] * self.store.dimensions for _ in chunks]
            chunk_ids = await self.store.async_add(
                chunks,
                zero_embs,
                source=source,
                user_id=user_id,
                metadatas=metadatas,
            )
            logger.debug("FTS-only ingest: %d chunks (no embeddings)", len(chunks))

        # Update in-memory BM25 as fallback
        if not self._use_fts5:
            self._bm25.add(chunk_ids, chunks)

        logger.info(
            "Ingested %d chunks from source=%r for user=%r",
            len(chunk_ids),
            source,
            user_id,
        )
        return chunk_ids

    _MAX_TOP_K = 1000

    async def query(
        self,
        query: str,
        *,
        top_k: int = 5,
        user_id: str | None = None,
        source: str | None = None,
    ) -> RAGResult:
        """Retrieve relevant chunks using hybrid search.

        Combines vector similarity (semantic) with keyword scores (FTS5 or BM25)
        using weighted reciprocal rank fusion.

        If no embedder is available, falls back to keyword-only search.
        """
        query = self._validate_str(query, "query", self._MAX_QUERY_LEN)
        if not query:
            return RAGResult(results=[], query=query, sources_used=[])
        if not isinstance(top_k, int) or top_k < 1:
            raise ValueError(f"top_k must be a positive integer, got {top_k!r}")
        if top_k > self._MAX_TOP_K:
            raise ValueError(
                f"top_k exceeds maximum ({top_k} > {self._MAX_TOP_K})"
            )
        if user_id is not None:
            user_id = self._validate_str(user_id, "user_id", self._MAX_USER_ID_LEN)
        if source is not None:
            source = self._validate_str(source, "source", self._MAX_SOURCE_LEN)
        vec_results: list[SearchResult] = []
        fused_scores: dict[str, float] = {}
        result_map: dict[str, SearchResult] = {}
        vec_weight = 1.0 - self.bm25_weight

        # Vector search (if embedder available)
        if self.embedder is not None:
            query_embedding = await self._embed_with_cache([query])
            vec_results = await self.store.async_search(
                query_embedding[0],
                top_k=top_k * 2,
                user_id=user_id,
                source=source,
            )

            for rank, result in enumerate(vec_results):
                rrf_score = vec_weight / (rank + 60)
                fused_scores[result.chunk_id] = fused_scores.get(result.chunk_id, 0) + rrf_score
                result_map[result.chunk_id] = result

        # Keyword search (FTS5 persistent or BM25 in-memory)
        keyword_hits: list[tuple[str, float]] = []
        if self._use_fts5:
            assert isinstance(self.store, SqliteVecStore)
            fts_results = self.store.search_fts(query, top_k=top_k * 2)
            keyword_hits = [(r.chunk_id, r.score) for r in fts_results]
            # Also populate result_map from FTS results
            for r in fts_results:
                if r.chunk_id not in result_map:
                    result_map[r.chunk_id] = r
        else:
            keyword_hits = self._bm25.search(query, top_k=top_k * 2)

        # Apply keyword weight to RRF fusion
        keyword_weight = self.bm25_weight if self.embedder is not None else 1.0
        for rank, (doc_id, _score) in enumerate(keyword_hits):
            rrf_score = keyword_weight / (rank + 60)
            fused_scores[doc_id] = fused_scores.get(doc_id, 0) + rrf_score
            if doc_id not in result_map:
                for vr in vec_results:
                    if vr.chunk_id == doc_id:
                        result_map[doc_id] = vr
                        break

        # Apply temporal decay: boost recent memories over old ones
        now = time.time()
        for chunk_id, base_score in fused_scores.items():
            result = result_map.get(chunk_id)
            if result is None:
                continue
            created_at = result.metadata.get("created_at")
            if created_at:
                age_days = max((now - created_at) / 86400, 0)
                # Gentle decay: score * (1 / (1 + age_days/30))
                # 1 day old → 0.97x, 7 days → 0.81x, 30 days → 0.50x, 90 days → 0.25x
                decay = 1.0 / (1.0 + age_days / 30.0)
                fused_scores[chunk_id] = base_score * decay

        # Sort by fused score (with temporal decay applied)
        ranked_ids = sorted(fused_scores, key=lambda x: fused_scores[x], reverse=True)

        # MMR deduplication: remove near-duplicate text results
        results: list[SearchResult] = []
        seen_texts: list[str] = []
        for chunk_id in ranked_ids:
            if len(results) >= top_k:
                break
            if chunk_id not in result_map:
                continue
            result = result_map[chunk_id]
            result.score = fused_scores[chunk_id]
            # Check text similarity against already-selected results
            if _is_redundant(result.text, seen_texts):
                continue
            results.append(result)
            seen_texts.append(result.text)

        sources = list({r.metadata.get("source", "") for r in results if r.metadata.get("source")})

        return RAGResult(results=results, query=query, sources_used=sources)

    async def delete_source(self, source: str) -> int:
        """Remove all chunks from a source."""
        count = self.store.delete_source(source)
        if not self._use_fts5:
            self._bm25.clear()
        logger.info("Deleted source=%r (%d chunks)", source, count)
        return count

    def list_sources(self) -> list[dict]:
        """List all ingested sources."""
        return self.store.list_sources()
