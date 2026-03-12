"""RAG engine that orchestrates embedding, storage, and retrieval."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from qanot.rag.chunker import BM25Index, chunk_text
from qanot.rag.embedder import Embedder
from qanot.rag.store import SearchResult, VectorStore

logger = logging.getLogger(__name__)


@dataclass
class RAGResult:
    """Combined result from hybrid retrieval."""

    results: list[SearchResult]
    query: str
    sources_used: list[str] = field(default_factory=list)


class RAGEngine:
    """Orchestrates chunking, embedding, storage, and hybrid retrieval.

    Combines vector similarity search with BM25 keyword matching
    for more robust retrieval.
    """

    def __init__(
        self,
        embedder: Embedder,
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
        self._bm25 = BM25Index()

    async def ingest(
        self,
        text: str,
        *,
        source: str = "",
        user_id: str = "",
        metadata: dict | None = None,
    ) -> list[str]:
        """Chunk, embed, and store a document.

        Args:
            text: Full document text.
            source: Source identifier (e.g., filename, URL).
            user_id: Owner of this content.
            metadata: Additional metadata per chunk.

        Returns:
            List of chunk IDs created.
        """
        chunks = chunk_text(
            text,
            max_tokens=self.chunk_size,
            overlap=self.chunk_overlap,
        )

        if not chunks:
            logger.debug("No chunks produced from source=%r", source)
            return []

        embeddings = await self.embedder.embed(chunks)
        metadatas = [metadata or {} for _ in chunks]

        chunk_ids = await self.store.async_add(
            chunks,
            embeddings,
            source=source,
            user_id=user_id,
            metadatas=metadatas,
        )

        # Update BM25 index
        self._bm25.add(chunk_ids, chunks)

        logger.info(
            "Ingested %d chunks from source=%r for user=%r",
            len(chunk_ids),
            source,
            user_id,
        )
        return chunk_ids

    async def query(
        self,
        query: str,
        *,
        top_k: int = 5,
        user_id: str | None = None,
        source: str | None = None,
    ) -> RAGResult:
        """Retrieve relevant chunks using hybrid search.

        Combines vector similarity (semantic) with BM25 (keyword) scores
        using weighted reciprocal rank fusion.

        Args:
            query: Search query text.
            top_k: Number of results to return.
            user_id: Filter by user.
            source: Filter by source.

        Returns:
            RAGResult with ranked results.
        """
        # Vector search
        query_embedding = await self.embedder.embed_single(query)
        vec_results = await self.store.async_search(
            query_embedding,
            top_k=top_k * 2,
            user_id=user_id,
            source=source,
        )

        # BM25 search
        bm25_hits = self._bm25.search(query, top_k=top_k * 2)
        bm25_scores = {doc_id: score for doc_id, score in bm25_hits}

        # Reciprocal rank fusion
        fused_scores: dict[str, float] = {}
        result_map: dict[str, SearchResult] = {}

        vec_weight = 1.0 - self.bm25_weight

        for rank, result in enumerate(vec_results):
            rrf_score = vec_weight / (rank + 60)  # k=60 for RRF
            fused_scores[result.chunk_id] = fused_scores.get(result.chunk_id, 0) + rrf_score
            result_map[result.chunk_id] = result

        for rank, (doc_id, _bm25_score) in enumerate(bm25_hits):
            rrf_score = self.bm25_weight / (rank + 60)
            fused_scores[doc_id] = fused_scores.get(doc_id, 0) + rrf_score
            # BM25 results may not have full SearchResult — use vec result if available
            if doc_id not in result_map:
                # Create a minimal result for BM25-only hits
                for vr in vec_results:
                    if vr.chunk_id == doc_id:
                        result_map[doc_id] = vr
                        break

        # Sort by fused score
        ranked_ids = sorted(fused_scores, key=lambda x: fused_scores[x], reverse=True)

        results: list[SearchResult] = []
        for chunk_id in ranked_ids[:top_k]:
            if chunk_id in result_map:
                result = result_map[chunk_id]
                result.score = fused_scores[chunk_id]
                results.append(result)

        # Collect source info
        sources = list({r.metadata.get("source", "") for r in results if r.metadata.get("source")})

        return RAGResult(results=results, query=query, sources_used=sources)

    async def delete_source(self, source: str) -> int:
        """Remove all chunks from a source."""
        count = self.store.delete_source(source)
        # Rebuild BM25 index (simple approach — rebuild from store)
        self._bm25.clear()
        logger.info("Deleted source=%r (%d chunks), BM25 index cleared", source, count)
        return count

    def list_sources(self) -> list[dict]:
        """List all ingested sources."""
        return self.store.list_sources()
