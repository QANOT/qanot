"""RAG (Retrieval-Augmented Generation) module for Qanot AI."""

from qanot.rag.chunker import BM25Index, chunk_text
from qanot.rag.embedder import Embedder, GeminiEmbedder, OpenAIEmbedder, create_embedder
from qanot.rag.engine import RAGEngine
from qanot.rag.indexer import MemoryIndexer
from qanot.rag.store import SearchResult, SqliteVecStore, VectorStore

__all__ = [
    "Embedder",
    "GeminiEmbedder",
    "OpenAIEmbedder",
    "create_embedder",
    "VectorStore",
    "SqliteVecStore",
    "SearchResult",
    "chunk_text",
    "BM25Index",
    "RAGEngine",
    "MemoryIndexer",
]
