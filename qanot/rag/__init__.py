"""RAG (Retrieval-Augmented Generation) module for Qanot AI."""

from __future__ import annotations

from qanot.rag.chunker import BM25Index, chunk_text
from qanot.rag.embedder import (
    create_embedder,
    Embedder,
    EmbedderHardError,
    EmbedderSoftError,
    GeminiEmbedder,
    OpenAIEmbedder,
)
from qanot.rag.engine import RAGEngine
from qanot.rag.indexer import MemoryIndexer
from qanot.rag.store import SearchResult, SqliteVecStore, VectorStore

__all__ = [
    "BM25Index",
    "chunk_text",
    "create_embedder",
    "Embedder",
    "EmbedderHardError",
    "EmbedderSoftError",
    "GeminiEmbedder",
    "MemoryIndexer",
    "OpenAIEmbedder",
    "RAGEngine",
    "SearchResult",
    "SqliteVecStore",
    "VectorStore",
]
