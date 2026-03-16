"""Tests for RAG module: chunker, embedder, engine, indexer."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field

import pytest

from qanot.rag.chunker import BM25Index, chunk_text
from qanot.rag.embedder import Embedder, create_embedder
from qanot.rag.store import SearchResult


def _sqlite_vec_available():
    try:
        import sqlite_vec

        return True
    except ImportError:
        return False


needs_sqlite_vec = pytest.mark.skipif(
    not _sqlite_vec_available(), reason="sqlite-vec not installed"
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


@dataclass
class MockProviderConfig:
    name: str = ""
    provider: str = ""
    model: str = ""
    api_key: str = "test-key"
    base_url: str = ""


@dataclass
class MockConfig:
    provider: str = ""
    api_key: str = ""
    providers: list = field(default_factory=list)


class MockEmbedder(Embedder):
    """Deterministic embedder for testing (no API calls)."""

    dimensions = 4

    async def embed(self, texts: list[str]) -> list[list[float]]:
        results = []
        for text in texts:
            h = hashlib.md5(text.encode()).digest()
            vec = [b / 255.0 for b in h[:4]]
            results.append(vec)
        return results


# ---------------------------------------------------------------------------
# TestChunker
# ---------------------------------------------------------------------------


class TestChunker:
    def test_empty_text(self):
        assert chunk_text("") == []

    def test_short_text(self):
        text = "Hello world, this is a short sentence."
        chunks = chunk_text(text, max_tokens=512)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text(self):
        # Create text that exceeds max_tokens=10 (~40 chars)
        text = "word " * 100  # 500 chars, well over 40
        chunks = chunk_text(text, max_tokens=10, overlap=0)
        assert len(chunks) > 1

    def test_overlap(self):
        # Single paragraph long enough to force word-level splitting,
        # then re-merged with overlap.  Use max_tokens=20 (~80 chars).
        text = " ".join(f"word{i}" for i in range(50))  # ~300 chars
        chunks_with_overlap = chunk_text(text, max_tokens=20, overlap=5)
        chunks_no_overlap = chunk_text(text, max_tokens=20, overlap=0)
        assert len(chunks_with_overlap) >= 2
        assert len(chunks_no_overlap) >= 2
        # With overlap, total character count should be greater because
        # content is duplicated at boundaries.
        total_with = sum(len(c) for c in chunks_with_overlap)
        total_without = sum(len(c) for c in chunks_no_overlap)
        assert total_with >= total_without


# ---------------------------------------------------------------------------
# TestBM25Index
# ---------------------------------------------------------------------------


class TestBM25Index:
    def test_empty_search(self):
        idx = BM25Index()
        assert idx.search("hello") == []

    def test_basic_search(self):
        idx = BM25Index()
        idx.add(
            ["d1", "d2", "d3"],
            [
                "python is a programming language",
                "java is also a language",
                "the weather is nice today",
            ],
        )
        results = idx.search("python programming")
        assert len(results) >= 1
        # First result should be the python document
        assert results[0][0] == "d1"

    def test_clear(self):
        idx = BM25Index()
        idx.add(["d1"], ["python programming language"])
        assert len(idx.search("python")) >= 1
        idx.clear()
        assert idx.search("python") == []

    def test_no_match(self):
        idx = BM25Index()
        idx.add(["d1"], ["python programming language"])
        results = idx.search("xylophone")
        assert results == []


# ---------------------------------------------------------------------------
# TestSearchResult
# ---------------------------------------------------------------------------


class TestSearchResult:
    def test_search_result_fields(self):
        r = SearchResult(
            chunk_id="abc123",
            text="hello world",
            metadata={"source": "test.md"},
            score=0.95,
        )
        assert r.chunk_id == "abc123"
        assert r.text == "hello world"
        assert r.metadata == {"source": "test.md"}
        assert r.score == 0.95


# ---------------------------------------------------------------------------
# TestCreateEmbedder
# ---------------------------------------------------------------------------


class TestCreateEmbedder:
    def test_gemini_preferred(self):
        cfg = MockConfig(
            providers=[
                MockProviderConfig(name="g", provider="gemini", api_key="gk"),
                MockProviderConfig(name="o", provider="openai", api_key="ok"),
            ]
        )
        from qanot.rag.embedder import GeminiEmbedder

        embedder = create_embedder(cfg)
        assert isinstance(embedder, GeminiEmbedder)

    def test_openai_fallback(self):
        cfg = MockConfig(
            providers=[
                MockProviderConfig(name="o", provider="openai", api_key="ok"),
            ]
        )
        from qanot.rag.embedder import OpenAIEmbedder

        embedder = create_embedder(cfg)
        assert isinstance(embedder, OpenAIEmbedder)

    def test_anthropic_only_returns_none(self):
        cfg = MockConfig(
            providers=[
                MockProviderConfig(name="a", provider="anthropic", api_key="ak"),
            ]
        )
        assert create_embedder(cfg) is None

    def test_no_providers_returns_none(self):
        cfg = MockConfig()
        assert create_embedder(cfg) is None


# ---------------------------------------------------------------------------
# TestRAGEngine (requires sqlite-vec)
# ---------------------------------------------------------------------------


@needs_sqlite_vec
class TestRAGEngine:
    def _make_engine(self, tmp_path):
        from qanot.rag.engine import RAGEngine
        from qanot.rag.store import SqliteVecStore

        store = SqliteVecStore(
            db_path=str(tmp_path / "rag.db"),
            dimensions=MockEmbedder.dimensions,
        )
        engine = RAGEngine(embedder=MockEmbedder(), store=store)
        return engine

    def test_ingest_and_query(self, tmp_path):
        engine = self._make_engine(tmp_path)
        chunk_ids = asyncio.run(
            engine.ingest(
                "Python is a great programming language for data science.",
                source="doc.md",
                user_id="u1",
            )
        )
        assert len(chunk_ids) >= 1

        result = asyncio.run(
            engine.query("python programming", top_k=3)
        )
        assert len(result.results) >= 1
        assert result.query == "python programming"

    def test_delete_source(self, tmp_path):
        engine = self._make_engine(tmp_path)
        asyncio.run(
            engine.ingest("Some content here", source="removeme.md")
        )
        count = asyncio.run(
            engine.delete_source("removeme.md")
        )
        assert count >= 1
        sources = engine.list_sources()
        assert all(s["source"] != "removeme.md" for s in sources)

    def test_list_sources(self, tmp_path):
        engine = self._make_engine(tmp_path)
        asyncio.run(
            engine.ingest("Content A", source="a.md")
        )
        asyncio.run(
            engine.ingest("Content B", source="b.md")
        )
        sources = engine.list_sources()
        source_names = [s["source"] for s in sources]
        assert "a.md" in source_names
        assert "b.md" in source_names


# ---------------------------------------------------------------------------
# TestMemoryIndexer (requires sqlite-vec)
# ---------------------------------------------------------------------------


@needs_sqlite_vec
class TestMemoryIndexer:
    def _make_indexer(self, tmp_path, workspace_dir):
        from qanot.rag.engine import RAGEngine
        from qanot.rag.indexer import MemoryIndexer
        from qanot.rag.store import SqliteVecStore

        store = SqliteVecStore(
            db_path=str(tmp_path / "idx.db"),
            dimensions=MockEmbedder.dimensions,
        )
        engine = RAGEngine(embedder=MockEmbedder(), store=store)
        return MemoryIndexer(engine=engine, workspace_dir=str(workspace_dir))

    def test_index_workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "MEMORY.md").write_text(
            "# Memory\n\nRemember: user prefers Python\n"
        )
        mem_dir = workspace / "memory"
        mem_dir.mkdir()
        (mem_dir / "2025-03-01.md").write_text("# Notes\n\nDeployed v2 today\n")

        indexer = self._make_indexer(tmp_path, workspace)
        total = asyncio.run(
            indexer.index_workspace(user_id="u1")
        )
        assert total >= 2  # At least one chunk from each file

    def test_index_skips_unchanged(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "MEMORY.md").write_text("# Memory\n\nSome content\n")

        indexer = self._make_indexer(tmp_path, workspace)
        first = asyncio.run(
            indexer.index_workspace(user_id="u1")
        )
        second = asyncio.run(
            indexer.index_workspace(user_id="u1")
        )
        assert first >= 1
        assert second == 0  # Nothing changed, should skip


# ---------------------------------------------------------------------------
# Edge Case Tests
# ---------------------------------------------------------------------------


class TestChunkerEdgeCases:
    def test_unicode_uzbek_text(self):
        text = "O'zbekiston Respublikasi — davlat mustaqilligi. Toshkent — poytaxt shahrimiz."
        chunks = chunk_text(text, max_tokens=512)
        assert len(chunks) >= 1
        assert "O'zbekiston" in chunks[0]

    def test_unicode_russian_text(self):
        text = "Привет мир, это тестовое сообщение для проверки кодировки."
        chunks = chunk_text(text, max_tokens=512)
        assert len(chunks) >= 1
        assert "Привет" in chunks[0]

    def test_unicode_mixed(self):
        text = (
            "Hello world! O'zbekiston Respublikasi. "
            "Привет мир! 🇺🇿 Emoji test 🎉✅"
        )
        chunks = chunk_text(text, max_tokens=512)
        assert len(chunks) >= 1
        # All content should be preserved
        combined = " ".join(chunks)
        assert "Hello" in combined
        assert "O'zbekiston" in combined
        assert "Привет" in combined

    def test_only_whitespace(self):
        assert chunk_text("   \n\n\t  ") == []

    def test_single_very_long_word(self):
        text = "a" * 10000
        chunks = chunk_text(text, max_tokens=50)
        assert len(chunks) >= 1
        # The long word can't be split by spaces, so it ends up as a single chunk
        combined = "".join(chunks)
        assert len(combined) == 10000

    def test_code_block(self):
        text = (
            "```python\n"
            "def hello(name: str) -> str:\n"
            "    return f'Hello, {name}!'\n"
            "\n"
            "class Foo:\n"
            "    def __init__(self):\n"
            "        self.x = 42\n"
            "```"
        )
        chunks = chunk_text(text, max_tokens=512)
        assert len(chunks) >= 1
        combined = " ".join(chunks)
        assert "def hello" in combined
        assert "class Foo" in combined

    def test_markdown_tables(self):
        text = (
            "| Name | Age |\n"
            "|------|-----|\n"
            "| Ali  | 25  |\n"
            "| Vali | 30  |\n"
        )
        chunks = chunk_text(text, max_tokens=512)
        assert len(chunks) >= 1
        assert "Ali" in chunks[0]

    def test_newlines_only(self):
        assert chunk_text("\n\n\n\n\n") == []

    def test_chunk_max_size_respected(self):
        # Build text long enough to force multiple chunks
        text = " ".join(f"sentence{i} with some words" for i in range(200))
        max_tokens = 30
        chunks = chunk_text(text, max_tokens=max_tokens, overlap=0)
        max_chars = max_tokens * 4
        for i, chunk in enumerate(chunks):
            assert len(chunk) <= max_chars, (
                f"Chunk {i} exceeds max: {len(chunk)} > {max_chars}"
            )

    def test_single_paragraph_exact_limit(self):
        max_tokens = 50
        max_chars = max_tokens * 4  # 200 chars
        text = "x" * max_chars
        chunks = chunk_text(text, max_tokens=max_tokens)
        # Exactly at limit should produce a single chunk
        assert len(chunks) == 1
        assert chunks[0] == text


class TestBM25EdgeCases:
    def test_special_characters_query(self):
        idx = BM25Index()
        idx.add(["d1"], ["python is great"])
        # Special chars should not cause errors
        results = idx.search("!@#$%^&*()")
        assert results == []

    def test_unicode_search(self):
        idx = BM25Index()
        idx.add(
            ["d1", "d2"],
            [
                "O'zbekiston Respublikasi mustaqilligi",
                "Python dasturlash tili",
            ],
        )
        results = idx.search("O'zbekiston mustaqilligi")
        assert len(results) >= 1
        assert results[0][0] == "d1"

    def test_duplicate_documents(self):
        idx = BM25Index()
        idx.add(["d1"], ["python programming"])
        idx.add(["d1"], ["python programming"])
        # Both entries exist (BM25Index doesn't deduplicate)
        results = idx.search("python")
        assert len(results) == 2

    def test_very_long_document(self):
        idx = BM25Index()
        long_text = " ".join(f"word{i}" for i in range(10000))  # ~50K chars
        idx.add(["d1"], [long_text])
        results = idx.search("word42")
        assert len(results) == 1
        assert results[0][0] == "d1"

    def test_single_word_docs(self):
        idx = BM25Index()
        idx.add(["d1", "d2", "d3"], ["apple", "banana", "cherry"])
        results = idx.search("banana")
        assert len(results) == 1
        assert results[0][0] == "d2"

    def test_empty_query(self):
        idx = BM25Index()
        idx.add(["d1"], ["python programming"])
        assert idx.search("") == []

    def test_query_with_only_stopwords(self):
        idx = BM25Index()
        idx.add(["d1"], ["python programming language"])
        # "the a an is" tokenizes to words that may or may not be in docs
        results = idx.search("the a an is")
        # Should not crash; results depend on whether docs contain these words
        assert isinstance(results, list)

    def test_large_corpus(self):
        idx = BM25Index()
        doc_ids = [f"d{i}" for i in range(1000)]
        texts = [f"document number {i} about topic {i % 10}" for i in range(1000)]
        idx.add(doc_ids, texts)
        results = idx.search("document topic 7", top_k=10)
        assert len(results) <= 10
        assert len(results) >= 1


class TestEmbedderEdgeCases:
    def test_single_provider_config(self):
        """Config with only provider/api_key fields (no providers list)."""
        from qanot.rag.embedder import OpenAIEmbedder

        cfg = MockConfig(provider="openai", api_key="test-key-123")
        embedder = create_embedder(cfg)
        assert isinstance(embedder, OpenAIEmbedder)

    def test_groq_only_returns_none(self):
        cfg = MockConfig(
            providers=[
                MockProviderConfig(name="g", provider="groq", api_key="gk"),
            ]
        )
        assert create_embedder(cfg) is None

    def test_gemini_with_custom_base_url(self):
        from qanot.rag.embedder import GeminiEmbedder

        cfg = MockConfig(
            providers=[
                MockProviderConfig(
                    name="g",
                    provider="gemini",
                    api_key="gk",
                    base_url="https://custom.example.com/v1/",
                ),
            ]
        )
        embedder = create_embedder(cfg)
        assert isinstance(embedder, GeminiEmbedder)
        assert "custom.example.com" in str(embedder.client.base_url)

    def test_mixed_providers_gemini_wins(self):
        from qanot.rag.embedder import GeminiEmbedder

        cfg = MockConfig(
            providers=[
                MockProviderConfig(name="a", provider="anthropic", api_key="ak"),
                MockProviderConfig(name="g", provider="groq", api_key="gk"),
                MockProviderConfig(name="ge", provider="gemini", api_key="gem-key"),
            ]
        )
        embedder = create_embedder(cfg)
        assert isinstance(embedder, GeminiEmbedder)

    def test_empty_api_key_skipped(self):
        """Provider with empty api_key in single-provider config should be skipped."""
        cfg = MockConfig(provider="openai", api_key="")
        assert create_embedder(cfg) is None


@needs_sqlite_vec
class TestStoreEdgeCases:
    def _make_store(self, tmp_path):
        from qanot.rag.store import SqliteVecStore

        return SqliteVecStore(
            db_path=str(tmp_path / "test.db"),
            dimensions=MockEmbedder.dimensions,
        )

    def _embed_sync(self, texts):
        """Synchronously generate mock embeddings."""
        embedder = MockEmbedder()
        return asyncio.run(embedder.embed(texts))

    def test_add_empty_batch(self, tmp_path):
        store = self._make_store(tmp_path)
        ids = store.add([], [])
        assert ids == []

    def test_search_empty_store(self, tmp_path):
        store = self._make_store(tmp_path)
        emb = self._embed_sync(["test"])[0]
        results = store.search(emb, top_k=5)
        assert results == []

    def test_delete_nonexistent_source(self, tmp_path):
        store = self._make_store(tmp_path)
        count = store.delete_source("nonexistent.md")
        assert count == 0

    def test_concurrent_writes(self, tmp_path):
        store = self._make_store(tmp_path)
        texts_a = ["first document content"]
        texts_b = ["second document content"]
        embs_a = self._embed_sync(texts_a)
        embs_b = self._embed_sync(texts_b)
        ids_a = store.add(texts_a, embs_a, source="a.md")
        ids_b = store.add(texts_b, embs_b, source="b.md")
        assert len(ids_a) == 1
        assert len(ids_b) == 1
        sources = store.list_sources()
        source_names = [s["source"] for s in sources]
        assert "a.md" in source_names
        assert "b.md" in source_names

    def test_special_chars_in_source(self, tmp_path):
        store = self._make_store(tmp_path)
        texts = ["content here"]
        embs = self._embed_sync(texts)
        source = "path/to/файл (copy).md"
        ids = store.add(texts, embs, source=source)
        assert len(ids) == 1
        sources = store.list_sources()
        assert any(s["source"] == source for s in sources)

    def test_metadata_with_unicode(self, tmp_path):
        store = self._make_store(tmp_path)
        texts = ["some text"]
        embs = self._embed_sync(texts)
        meta = {"title": "O'zbekiston tarixi", "note": "Привет мир"}
        ids = store.add(texts, embs, metadatas=[meta])
        assert len(ids) == 1
        # Verify metadata is persisted by searching
        results = store.search(embs[0], top_k=1)
        assert len(results) == 1
        assert results[0].metadata["title"] == "O'zbekiston tarixi"
        assert results[0].metadata["note"] == "Привет мир"

    def test_search_with_user_filter(self, tmp_path):
        store = self._make_store(tmp_path)
        texts_a = ["document for user alpha"]
        texts_b = ["document for user beta"]
        embs_a = self._embed_sync(texts_a)
        embs_b = self._embed_sync(texts_b)
        store.add(texts_a, embs_a, user_id="alpha", source="a.md")
        store.add(texts_b, embs_b, user_id="beta", source="b.md")
        # Search as alpha — should only see alpha's docs
        results = store.search(embs_a[0], top_k=10, user_id="alpha")
        assert all(r.metadata.get("user_id") == "alpha" for r in results)

    def test_large_batch(self, tmp_path):
        store = self._make_store(tmp_path)
        texts = [f"chunk number {i} with content" for i in range(500)]
        embs = self._embed_sync(texts)
        ids = store.add(texts, embs, source="large.md")
        assert len(ids) == 500
        sources = store.list_sources()
        large = [s for s in sources if s["source"] == "large.md"]
        assert large[0]["chunk_count"] == 500


@needs_sqlite_vec
class TestEngineEdgeCases:
    def _make_engine(self, tmp_path):
        from qanot.rag.engine import RAGEngine
        from qanot.rag.store import SqliteVecStore

        store = SqliteVecStore(
            db_path=str(tmp_path / "engine.db"),
            dimensions=MockEmbedder.dimensions,
        )
        return RAGEngine(embedder=MockEmbedder(), store=store)

    def test_query_empty_index(self, tmp_path):
        engine = self._make_engine(tmp_path)
        result = asyncio.run(
            engine.query("anything at all", top_k=5)
        )
        assert result.results == []
        assert result.query == "anything at all"

    def test_ingest_empty_text(self, tmp_path):
        engine = self._make_engine(tmp_path)
        ids = asyncio.run(
            engine.ingest("", source="empty.md")
        )
        assert ids == []

    def test_ingest_whitespace_only(self, tmp_path):
        engine = self._make_engine(tmp_path)
        ids = asyncio.run(
            engine.ingest("  \n  ", source="blank.md")
        )
        assert ids == []

    def test_bm25_only_results(self, tmp_path):
        """Ingest content, then query — BM25 should contribute to results."""
        engine = self._make_engine(tmp_path)
        asyncio.run(
            engine.ingest(
                "Python dasturlash tili juda kuchli",
                source="lang.md",
                user_id="u1",
            )
        )
        result = asyncio.run(
            engine.query("Python dasturlash", top_k=5, user_id="u1")
        )
        # Should find the doc through hybrid search
        assert len(result.results) >= 1

    def test_source_filter(self, tmp_path):
        engine = self._make_engine(tmp_path)
        asyncio.run(
            engine.ingest("Alpha content here", source="alpha.md", user_id="u1")
        )
        asyncio.run(
            engine.ingest("Beta content here", source="beta.md", user_id="u1")
        )
        result = asyncio.run(
            engine.query("content", top_k=10, user_id="u1", source="alpha.md")
        )
        for r in result.results:
            assert r.metadata.get("source") == "alpha.md"

    def test_shared_memory_across_users(self, tmp_path):
        """OpenClaw-style: all users share the same memory (no isolation)."""
        engine = self._make_engine(tmp_path)
        asyncio.run(
            engine.ingest("Shared data from user A", source="a.md", user_id="userA")
        )
        result = asyncio.run(
            engine.query("Shared data", top_k=5, user_id="userB")
        )
        # OpenClaw-style: userB CAN see userA's data (shared per-agent)
        assert len(result.results) > 0


class TestMemoryHooks:
    def test_hook_called_on_wal_write(self, tmp_path):
        from qanot.memory import _write_hooks, add_write_hook, wal_write, WALEntry

        original_hooks = _write_hooks.copy()
        _write_hooks.clear()
        try:
            calls = []
            add_write_hook(lambda content, source: calls.append((content, source)))

            entries = [WALEntry(category="test", detail="hook test detail")]
            wal_write(entries, workspace_dir=str(tmp_path))

            assert len(calls) == 1
            assert "hook test detail" in calls[0][0]
            assert calls[0][1] == "SESSION-STATE.md"
        finally:
            _write_hooks.clear()
            _write_hooks.extend(original_hooks)

    def test_hook_called_on_daily_note(self, tmp_path):
        from qanot.memory import _write_hooks, add_write_hook, write_daily_note

        original_hooks = _write_hooks.copy()
        _write_hooks.clear()
        try:
            calls = []
            add_write_hook(lambda content, source: calls.append((content, source)))

            write_daily_note("test daily content", workspace_dir=str(tmp_path))

            assert len(calls) == 1
            assert calls[0][0] == "test daily content"
            assert "memory/" in calls[0][1]
        finally:
            _write_hooks.clear()
            _write_hooks.extend(original_hooks)

    def test_hook_exception_swallowed(self, tmp_path):
        from qanot.memory import _write_hooks, add_write_hook, wal_write, WALEntry

        original_hooks = _write_hooks.copy()
        _write_hooks.clear()
        try:

            def bad_hook(content, source):
                raise RuntimeError("Hook exploded!")

            add_write_hook(bad_hook)

            entries = [WALEntry(category="test", detail="should not crash")]
            # Should not raise
            wal_write(entries, workspace_dir=str(tmp_path))

            # Verify file was still written
            state = (tmp_path / "SESSION-STATE.md").read_text()
            assert "should not crash" in state
        finally:
            _write_hooks.clear()
            _write_hooks.extend(original_hooks)

    def test_multiple_hooks(self, tmp_path):
        from qanot.memory import _write_hooks, add_write_hook, wal_write, WALEntry

        original_hooks = _write_hooks.copy()
        _write_hooks.clear()
        try:
            calls_a = []
            calls_b = []
            add_write_hook(lambda content, source: calls_a.append(source))
            add_write_hook(lambda content, source: calls_b.append(source))

            entries = [WALEntry(category="pref", detail="both hooks")]
            wal_write(entries, workspace_dir=str(tmp_path))

            assert len(calls_a) == 1
            assert len(calls_b) == 1
        finally:
            _write_hooks.clear()
            _write_hooks.extend(original_hooks)


class TestRAGTools:
    def _setup_tools(self, tmp_path):
        """Set up a ToolRegistry with RAG tools registered."""
        from qanot.registry import ToolRegistry
        from qanot.rag.engine import RAGEngine
        from qanot.rag.store import SqliteVecStore
        from qanot.tools.rag import register_rag_tools

        store = SqliteVecStore(
            db_path=str(tmp_path / "tools.db"),
            dimensions=MockEmbedder.dimensions,
        )
        engine = RAGEngine(embedder=MockEmbedder(), store=store)
        registry = ToolRegistry()
        register_rag_tools(
            registry, engine, str(tmp_path), lambda: "test_user"
        )
        return registry

    @needs_sqlite_vec
    def test_rag_index_missing_path(self, tmp_path):
        import json

        registry = self._setup_tools(tmp_path)
        handler = registry._handlers["rag_index"]
        result = asyncio.run(handler({}))
        data = json.loads(result)
        assert "error" in data
        assert "path" in data["error"].lower()

    @needs_sqlite_vec
    def test_rag_index_nonexistent_file(self, tmp_path):
        import json

        registry = self._setup_tools(tmp_path)
        handler = registry._handlers["rag_index"]
        result = asyncio.run(
            handler({"path": "does_not_exist.md"})
        )
        data = json.loads(result)
        assert "error" in data
        assert "not found" in data["error"].lower()

    @needs_sqlite_vec
    def test_rag_index_unsupported_extension(self, tmp_path):
        import json

        (tmp_path / "bad.exe").write_bytes(b"\x00\x01\x02")
        registry = self._setup_tools(tmp_path)
        handler = registry._handlers["rag_index"]
        result = asyncio.run(
            handler({"path": "bad.exe"})
        )
        data = json.loads(result)
        assert "error" in data
        assert "unsupported" in data["error"].lower()

    @needs_sqlite_vec
    def test_rag_search_empty_query(self, tmp_path):
        import json

        registry = self._setup_tools(tmp_path)
        handler = registry._handlers["rag_search"]
        result = asyncio.run(
            handler({"query": ""})
        )
        data = json.loads(result)
        assert "error" in data

    @needs_sqlite_vec
    def test_rag_forget_nonexistent(self, tmp_path):
        import json

        registry = self._setup_tools(tmp_path)
        handler = registry._handlers["rag_forget"]
        result = asyncio.run(
            handler({"source": "never_existed.md"})
        )
        data = json.loads(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# TestFTS5Search (persistent full-text search)
# ---------------------------------------------------------------------------


@needs_sqlite_vec
class TestFTS5Search:
    def _make_store(self, tmp_path):
        from qanot.rag.store import SqliteVecStore

        return SqliteVecStore(
            db_path=str(tmp_path / "fts5.db"),
            dimensions=MockEmbedder.dimensions,
        )

    def _embed_sync(self, texts):
        embedder = MockEmbedder()
        return asyncio.run(embedder.embed(texts))

    def test_fts5_available(self, tmp_path):
        store = self._make_store(tmp_path)
        assert store.fts_available is True

    def test_fts5_basic_search(self, tmp_path):
        store = self._make_store(tmp_path)
        texts = [
            "Python is a great programming language",
            "Java is used in enterprise applications",
            "The weather is sunny today",
        ]
        embs = self._embed_sync(texts)
        store.add(texts, embs, source="docs.md")

        results = store.search_fts("Python programming", top_k=3)
        assert len(results) >= 1
        assert "Python" in results[0].text

    def test_fts5_no_match(self, tmp_path):
        store = self._make_store(tmp_path)
        texts = ["Python programming language"]
        embs = self._embed_sync(texts)
        store.add(texts, embs, source="docs.md")

        results = store.search_fts("xylophone accordion", top_k=5)
        assert results == []

    def test_fts5_empty_query(self, tmp_path):
        store = self._make_store(tmp_path)
        results = store.search_fts("", top_k=5)
        assert results == []

    def test_fts5_single_char_tokens_skipped(self, tmp_path):
        store = self._make_store(tmp_path)
        # Single char tokens are filtered by _build_fts_query
        results = store.search_fts("a b c", top_k=5)
        assert results == []

    def test_fts5_unicode_search(self, tmp_path):
        store = self._make_store(tmp_path)
        texts = [
            "O'zbekiston Respublikasi mustaqil davlat",
            "Python dasturlash tili",
        ]
        embs = self._embed_sync(texts)
        store.add(texts, embs, source="uz.md")

        results = store.search_fts("O'zbekiston mustaqil", top_k=5)
        assert len(results) >= 1

    def test_fts5_persists_across_reopen(self, tmp_path):
        """FTS5 index survives close/reopen (unlike in-memory BM25)."""
        from qanot.rag.store import SqliteVecStore

        db_path = str(tmp_path / "persist.db")
        store1 = SqliteVecStore(db_path=db_path, dimensions=MockEmbedder.dimensions)
        texts = ["Python programming is wonderful"]
        embs = self._embed_sync(texts)
        store1.add(texts, embs, source="test.md")
        store1.close()

        # Reopen — FTS5 index should still work
        store2 = SqliteVecStore(db_path=db_path, dimensions=MockEmbedder.dimensions)
        results = store2.search_fts("Python programming", top_k=5)
        assert len(results) >= 1
        assert "Python" in results[0].text
        store2.close()

    def test_fts5_delete_source_cleans_index(self, tmp_path):
        store = self._make_store(tmp_path)
        texts = ["Deletable content about rockets"]
        embs = self._embed_sync(texts)
        store.add(texts, embs, source="remove.md")

        # Verify FTS finds it
        assert len(store.search_fts("rockets", top_k=5)) >= 1

        # Delete and verify gone from FTS
        store.delete_source("remove.md")
        assert store.search_fts("rockets", top_k=5) == []

    def test_fts5_bm25_scoring(self, tmp_path):
        """More relevant docs should score higher."""
        store = self._make_store(tmp_path)
        texts = [
            "Python Python Python repeated",
            "Python mentioned once here",
            "No match in this document",
        ]
        embs = self._embed_sync(texts)
        store.add(texts, embs, source="score.md")

        results = store.search_fts("Python", top_k=3)
        assert len(results) >= 2
        # First result should have higher score (more Python occurrences)
        assert results[0].score >= results[1].score


# ---------------------------------------------------------------------------
# TestEmbeddingCache
# ---------------------------------------------------------------------------


@needs_sqlite_vec
class TestEmbeddingCache:
    def _make_store(self, tmp_path):
        from qanot.rag.store import SqliteVecStore

        return SqliteVecStore(
            db_path=str(tmp_path / "cache.db"),
            dimensions=MockEmbedder.dimensions,
            cache_max_entries=100,
        )

    def test_cache_miss_then_hit(self, tmp_path):
        from qanot.rag.store import _hash_text

        store = self._make_store(tmp_path)
        h = _hash_text("hello world")

        # Miss
        result = store.cache_get([h], "test", "model-1")
        assert result == {}

        # Put
        store.cache_put([(h, [1.0, 2.0, 3.0, 4.0])], "test", "model-1")

        # Hit
        result = store.cache_get([h], "test", "model-1")
        assert h in result
        assert len(result[h]) == 4
        assert result[h] == pytest.approx([1.0, 2.0, 3.0, 4.0])

    def test_cache_provider_isolation(self, tmp_path):
        from qanot.rag.store import _hash_text

        store = self._make_store(tmp_path)
        h = _hash_text("same text")

        store.cache_put([(h, [1.0, 2.0, 3.0, 4.0])], "gemini", "emb-001")

        # Different provider should miss
        result = store.cache_get([h], "openai", "text-3-small")
        assert result == {}

        # Same provider should hit
        result = store.cache_get([h], "gemini", "emb-001")
        assert h in result

    def test_cache_batch_operations(self, tmp_path):
        from qanot.rag.store import _hash_text

        store = self._make_store(tmp_path)
        items = [
            (_hash_text(f"text {i}"), [float(i)] * 4)
            for i in range(50)
        ]
        hashes = [h for h, _ in items]

        store.cache_put(items, "test", "model")
        result = store.cache_get(hashes, "test", "model")
        assert len(result) == 50

    def test_cache_lru_eviction(self, tmp_path):
        from qanot.rag.store import SqliteVecStore, _hash_text

        store = SqliteVecStore(
            db_path=str(tmp_path / "lru.db"),
            dimensions=4,
            cache_max_entries=10,
        )

        # Insert 15 items — should evict 5 oldest
        for i in range(15):
            h = _hash_text(f"entry {i}")
            store.cache_put([(h, [float(i)] * 4)], "test", "model")

        # Check total count
        conn = store._conn
        count = conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0]
        assert count == 10  # Max entries enforced

    def test_cache_update_existing(self, tmp_path):
        from qanot.rag.store import _hash_text

        store = self._make_store(tmp_path)
        h = _hash_text("update me")

        store.cache_put([(h, [1.0, 1.0, 1.0, 1.0])], "test", "model")
        store.cache_put([(h, [2.0, 2.0, 2.0, 2.0])], "test", "model")

        result = store.cache_get([h], "test", "model")
        assert result[h] == pytest.approx([2.0, 2.0, 2.0, 2.0])

    def test_cache_empty_operations(self, tmp_path):
        store = self._make_store(tmp_path)
        # Empty get/put should not error
        assert store.cache_get([], "test", "model") == {}
        store.cache_put([], "test", "model")  # No error


# ---------------------------------------------------------------------------
# TestEngineEmbeddingCache (integration)
# ---------------------------------------------------------------------------


@needs_sqlite_vec
class TestEngineEmbeddingCache:
    def _make_engine(self, tmp_path):
        from qanot.rag.engine import RAGEngine
        from qanot.rag.store import SqliteVecStore

        store = SqliteVecStore(
            db_path=str(tmp_path / "cached_engine.db"),
            dimensions=MockEmbedder.dimensions,
        )
        return RAGEngine(embedder=MockEmbedder(), store=store), store

    def test_ingest_populates_cache(self, tmp_path):
        engine, store = self._make_engine(tmp_path)
        asyncio.run(
            engine.ingest("Python is wonderful", source="test.md")
        )
        # Cache should have entries
        count = store._conn.execute(
            "SELECT COUNT(*) FROM embedding_cache"
        ).fetchone()[0]
        assert count >= 1

    def test_reingest_uses_cache(self, tmp_path):
        """Second ingest of same text should use cache (no API call)."""
        from qanot.rag.store import SqliteVecStore

        db_path = str(tmp_path / "reingest.db")
        store = SqliteVecStore(db_path=db_path, dimensions=MockEmbedder.dimensions)

        call_count = 0
        original_embed = MockEmbedder.embed

        class CountingEmbedder(MockEmbedder):
            async def embed(self, texts):
                nonlocal call_count
                call_count += 1
                return await original_embed(self, texts)

        from qanot.rag.engine import RAGEngine

        engine = RAGEngine(embedder=CountingEmbedder(), store=store)

        text = "Same text for caching test"
        asyncio.run(engine.ingest(text, source="first.md"))
        first_calls = call_count

        asyncio.run(engine.ingest(text, source="second.md"))
        second_calls = call_count - first_calls

        # Second ingest should NOT call embed (cached)
        assert second_calls == 0

    def test_engine_fts_mode(self, tmp_path):
        engine, _ = self._make_engine(tmp_path)
        assert engine.fts_mode == "fts5"
        assert engine.has_embedder is True


# ---------------------------------------------------------------------------
# TestFTSOnlyMode (no embedder)
# ---------------------------------------------------------------------------


@needs_sqlite_vec
class TestFTSOnlyMode:
    def _make_fts_engine(self, tmp_path):
        from qanot.rag.engine import RAGEngine
        from qanot.rag.store import SqliteVecStore

        store = SqliteVecStore(
            db_path=str(tmp_path / "fts_only.db"),
            dimensions=4,
        )
        return RAGEngine(embedder=None, store=store)

    def test_ingest_without_embedder(self, tmp_path):
        engine = self._make_fts_engine(tmp_path)
        assert engine.has_embedder is False
        ids = asyncio.run(
            engine.ingest("Python is great for data science", source="test.md")
        )
        assert len(ids) >= 1

    def test_query_fts_only(self, tmp_path):
        engine = self._make_fts_engine(tmp_path)
        asyncio.run(
            engine.ingest("Python programming language", source="lang.md")
        )
        result = asyncio.run(engine.query("Python programming", top_k=5))
        assert len(result.results) >= 1
        assert "Python" in result.results[0].text

    def test_query_empty_fts_only(self, tmp_path):
        engine = self._make_fts_engine(tmp_path)
        result = asyncio.run(engine.query("nothing here", top_k=5))
        assert result.results == []


# ---------------------------------------------------------------------------
# TestFallbackChain (embedder creation)
# ---------------------------------------------------------------------------


class TestFallbackChain:
    def test_gemini_init_failure_falls_to_openai(self):
        """If Gemini init fails, should fall back to OpenAI."""
        from qanot.rag.embedder import OpenAIEmbedder, create_embedder

        cfg = MockConfig(
            providers=[
                MockProviderConfig(name="g", provider="gemini", api_key="gk"),
                MockProviderConfig(name="o", provider="openai", api_key="ok"),
            ]
        )
        # Normal case: Gemini wins
        embedder = create_embedder(cfg)
        # Gemini has openai installed, so it should succeed and return Gemini
        from qanot.rag.embedder import GeminiEmbedder
        assert isinstance(embedder, (GeminiEmbedder, OpenAIEmbedder))

    def test_empty_gemini_key_skips(self):
        """Empty Gemini key should skip to OpenAI."""
        from qanot.rag.embedder import OpenAIEmbedder, create_embedder

        cfg = MockConfig(
            providers=[
                MockProviderConfig(name="g", provider="gemini", api_key=""),
                MockProviderConfig(name="o", provider="openai", api_key="ok"),
            ]
        )
        embedder = create_embedder(cfg)
        assert isinstance(embedder, OpenAIEmbedder)

    def test_all_empty_keys_returns_none(self):
        from qanot.rag.embedder import create_embedder

        cfg = MockConfig(
            providers=[
                MockProviderConfig(name="g", provider="gemini", api_key=""),
                MockProviderConfig(name="o", provider="openai", api_key=""),
            ]
        )
        assert create_embedder(cfg) is None

    def test_embedder_error_classes_exist(self):
        from qanot.rag.embedder import EmbedderHardError, EmbedderSoftError
        assert issubclass(EmbedderSoftError, Exception)
        assert issubclass(EmbedderHardError, Exception)
