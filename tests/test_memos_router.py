"""Tests for qanot.memos.router — scope filter + embedding selection.

Uses a deterministic stub embedder so tests are fast and reproducible.
The real FastEmbedder is exercised in production; the contract that
router depends on (``async embed(texts) -> list[list[float]]``) is
trivial enough to stub.
"""

from __future__ import annotations

import asyncio

import pytest

from qanot.memos import MemoStore, MemoType
from qanot.memos.router import (
    DEFAULT_THRESHOLD,
    MemoRouter,
    Selection,
    _cosine,
)


class StubEmbedder:
    """Maps each input text to a deterministic vector via a tiny token bag.

    The vector is a sparse one-hot over a known vocabulary. Cosine
    similarity then reflects shared token count — useful for assertions
    without depending on a real embedding model.
    """

    VOCAB = [
        "title", "format", "daily", "ielts", "academic", "english",
        "color", "name", "user", "trading", "gold", "xauusd",
        "create", "write", "today", "tomorrow", "rule", "always",
    ]

    def __init__(self):
        self.call_count = 0
        self.batch_sizes: list[int] = []

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        self.batch_sizes.append(len(texts))
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        text_low = text.lower()
        return [1.0 if word in text_low else 0.0 for word in self.VOCAB]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store_with_memos(tmp_path):
    store = MemoStore(tmp_path)
    # Mix of scopes so we can assert pre-filter behavior.
    store.upsert(
        "feedback-title-format",
        "Daily note title must use D-month YYYY format",
        MemoType.FEEDBACK,
        "Title format example: 12-may, 2026",
        thread_scope="kunlik-yozuv",
    )
    store.upsert(
        "feedback-ielts-style",
        "Use academic English for IELTS preparation prompts",
        MemoType.FEEDBACK,
        "Respond in academic register, B2+ vocabulary",
        thread_scope="ielts",
    )
    store.upsert(
        "user-color",
        "User's favorite color is blue",
        MemoType.USER,
        "Favorite color: ko'k",
        user_scope="1545224574",
    )
    store.upsert(
        "user-trading-focus",
        "Trading bot work centers on XAUUSD gold scalping",
        MemoType.PROJECT,
        "XAUUSD scalping on M5/M15 with ATR + EMA",
    )
    return store


@pytest.fixture
def embedder():
    return StubEmbedder()


# ─── scope filtering ─────────────────────────────────────────────


class TestScopeFilter:
    def test_global_caller_sees_only_global_memos(self, store_with_memos, embedder):
        router = MemoRouter(store_with_memos, embedder, threshold=0.0)
        # No user/thread → only the unscoped trading memo is a candidate.
        result = _run(router.route("trading gold xauusd"))
        names = {s.memo.name for s in result.selections}
        assert names == {"user-trading-focus"}
        assert result.candidates == 1

    def test_thread_filter_excludes_other_threads(self, store_with_memos, embedder):
        router = MemoRouter(store_with_memos, embedder, threshold=0.0)
        # Caller in IELTS thread — title-format and trading memos eligible.
        result = _run(router.route(
            "academic english title", thread_id="ielts",
        ))
        names = {s.memo.name for s in result.selections}
        assert "feedback-title-format" not in names  # wrong thread
        # IELTS style memo should be in candidates regardless of selection.
        assert result.candidates >= 1

    def test_user_filter(self, store_with_memos, embedder):
        router = MemoRouter(store_with_memos, embedder, threshold=0.0)
        # The user-color memo is user-scoped to 1545224574.
        wrong_user = _run(router.route("color", user_id="other"))
        names = {s.memo.name for s in wrong_user.selections}
        assert "user-color" not in names

        right_user = _run(router.route("color", user_id="1545224574"))
        names = {s.memo.name for s in right_user.selections}
        assert "user-color" in names


# ─── embedding selection ────────────────────────────────────────


class TestEmbeddingSelection:
    def test_top_k_respected(self, store_with_memos, embedder):
        router = MemoRouter(
            store_with_memos, embedder, threshold=0.0, top_k=1,
        )
        # Ample candidate pool in IELTS thread.
        result = _run(router.route(
            "title format daily ielts academic", thread_id="ielts",
            user_id="1545224574",
        ))
        assert len(result.selections) <= 1

    def test_threshold_drops_low_scores(self, store_with_memos, embedder):
        router = MemoRouter(
            store_with_memos, embedder, threshold=0.5,
        )
        # "weather" shares no vocab tokens with any memo description.
        result = _run(router.route("weather forecast"))
        assert result.selections == []
        assert result.candidates == 1  # the global trading memo
        assert result.above_threshold == 0

    def test_threshold_zero_keeps_all_in_top_k(self, store_with_memos, embedder):
        router = MemoRouter(
            store_with_memos, embedder, threshold=0.0, top_k=10,
        )
        result = _run(router.route(
            "create daily note today",
            thread_id="kunlik-yozuv",
        ))
        # Both global trading memo and thread-scoped title-format memo
        # are candidates; even at threshold 0 with stub embedder, the
        # title-format memo scores higher (shares "title", "daily").
        assert result.selections, "expected at least one selection"
        # Top result should be the title-format memo by token overlap.
        assert result.selections[0].memo.name == "feedback-title-format"

    def test_budget_cap_drops_low_scoring(self, tmp_path, embedder):
        store = MemoStore(tmp_path)
        # Three memos, each with a long body.
        long_body = "x " * 2000  # ~4000 chars
        for i in range(3):
            store.upsert(
                f"memo-{i}",
                f"description {'title' if i == 0 else 'unused'}",
                MemoType.USER, long_body,
            )
        router = MemoRouter(
            store, embedder, threshold=0.0, top_k=10,
            budget_chars=5000,  # only fits one long memo
        )
        result = _run(router.route("title format"))
        # At most one memo fits the budget; the rest are dropped.
        kept_chars = sum(len(s.memo.body) for s in result.selections)
        assert kept_chars <= 5000
        assert result.dropped_for_budget >= 1


# ─── embedding cache ────────────────────────────────────────────


class TestEmbeddingCache:
    def test_repeat_calls_dont_re_embed(self, store_with_memos, embedder):
        router = MemoRouter(store_with_memos, embedder, threshold=0.0)
        _run(router.route("title format daily"))
        first_calls = embedder.call_count
        _run(router.route("another query"))
        # The second call only embeds the new user message — descriptions
        # were cached after the first call.
        assert embedder.call_count == first_calls + 1
        # Last batch was a 1-element batch (the user message only).
        assert embedder.batch_sizes[-1] == 1

    def test_updated_description_invalidates_cache(self, store_with_memos, embedder):
        router = MemoRouter(store_with_memos, embedder, threshold=0.0)
        _run(router.route("daily title"))
        first_calls = embedder.call_count
        # Update one memo's description — cache miss next time.
        store_with_memos.upsert(
            "feedback-title-format",
            "Now describes something completely different",
            MemoType.FEEDBACK,
            "Body updated",
            thread_scope="kunlik-yozuv",
        )
        _run(router.route("daily title", thread_id="kunlik-yozuv"))
        # One additional embed call for the updated description (plus
        # the user message).
        assert embedder.call_count > first_calls


# ─── failure mode ───────────────────────────────────────────────


class TestFailureMode:
    def test_embed_failure_returns_empty(self, store_with_memos):
        async def broken_embed(_texts):
            raise RuntimeError("embed broken")

        router = MemoRouter(store_with_memos, broken_embed, threshold=0.0)
        result = _run(router.route("anything"))
        # Router degrades gracefully: zero selections, but the call returns.
        assert result.selections == []

    def test_blank_message_short_circuits(self, store_with_memos, embedder):
        router = MemoRouter(store_with_memos, embedder, threshold=0.0)
        result = _run(router.route(""))
        assert result.selections == []
        assert embedder.call_count == 0  # never even called


# ─── cosine helper ──────────────────────────────────────────────


class TestCosine:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 1.0]
        assert _cosine(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector_safe(self):
        assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
        assert _cosine([1.0, 1.0], [0.0, 0.0]) == 0.0

    def test_length_mismatch_returns_zero(self):
        assert _cosine([1.0], [1.0, 1.0]) == 0.0
