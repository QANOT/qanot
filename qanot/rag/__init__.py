"""RAG (Retrieval-Augmented Generation) module for Qanot AI."""

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
    "chunk_text",
    "create_embedder",
]
