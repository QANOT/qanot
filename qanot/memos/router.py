"""Memo router — selects up to N memos per turn for ``<system-reminder>`` injection.

Two-stage selection, in this order:

  1. **Scope pre-filter** (free): drop memos whose ``metadata.user`` or
     ``metadata.thread`` don't match the current call. Pure equality check.
  2. **Embedding retrieval** (cheap): cosine-similarity between the user
     message and each candidate's ``description`` field. Top-K above a
     threshold get injected. Uses the same FastEmbed instance the RAG
     layer already pays for, so the marginal cost is ~5ms CPU per turn.

We chose embedding retrieval over Claude Code's LLM-as-router after the
2026-05-13 cost-review:

  - LLM router (Haiku): ~$0.0001/turn × 100 turns/day = $0.30/month
    per active user. Acceptable but stacks across the whole bot fleet.
  - Embedding retrieval: $0 (local CPU). At our memo counts (<200 per
    workspace, far below the 50K where LLM ranking starts winning),
    cosine on ``description`` gives near-identical relevance ranking.

The router NEVER injects an out-of-scope memo even if the threshold
trips — scope filtering is the safety guarantee that the title-format
rule for the daily-notes thread doesn't leak into the IELTS thread.

This module is async because the FastEmbed wrapper is async; the
underlying CPU embed is sync (run via ``asyncio.to_thread`` upstream).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Awaitable, Callable

from .spec import MemoSpec
from .store import MemoStore

logger = logging.getLogger(__name__)


# Defaults. Each is a knob: lower the threshold for more reminders per
# turn (better adherence, more tokens); raise it for tighter budgets.
DEFAULT_TOP_K = 5
DEFAULT_THRESHOLD = 0.45  # cosine similarity; calibrated for nomic-embed-v1.5
# Hard cap on context budget the router can add to a single turn,
# expressed in characters of memo body (not tokens — we approximate at
# ~4 chars/token, so 8000 chars ≈ 2000 tokens). When the selected
# memos exceed this, we drop the lowest-scoring ones until we fit.
DEFAULT_BUDGET_CHARS = 8_000


# Type alias for the embed callable. ``embed(texts)`` returns a list of
# float vectors, one per input. We require an async signature so the
# router doesn't have to know how the wrapper is implemented.
EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


@dataclass
class Selection:
    """One memo + the similarity score that landed it in the top-K."""

    memo: MemoSpec
    score: float


@dataclass
class RouteResult:
    """Output of a single ``route`` call."""

    selections: list[Selection]
    # Telemetry for the caller — useful for logging and cost-tracking.
    candidates: int       # in-scope memos considered after pre-filter
    above_threshold: int  # candidates whose score cleared the threshold
    dropped_for_budget: int  # dropped because total chars > budget


# ─── public API ──────────────────────────────────────────────────


class MemoRouter:
    """Per-turn memo selection via scope filter + embedding retrieval.

    Construct once per workspace; reuse across turns. The embedding
    cache is held in-memory keyed by ``(memo.name, body_hash)`` — when
    a memo is updated, its embedding is recomputed lazily on the next
    ``route`` call.

    `embed` is the async embedding function — typically
    ``rag_embedder.embed``. We accept it as a callable rather than a
    concrete class so tests can stub it.
    """

    def __init__(
        self,
        store: MemoStore,
        embed: EmbedFn,
        *,
        top_k: int = DEFAULT_TOP_K,
        threshold: float = DEFAULT_THRESHOLD,
        budget_chars: int = DEFAULT_BUDGET_CHARS,
    ):
        self.store = store
        self._embed = embed
        self.top_k = top_k
        self.threshold = threshold
        self.budget_chars = budget_chars

        # name → (description, vector). Re-keyed on every route call so a
        # memo description edit invalidates the entry.
        self._cache: dict[str, tuple[str, list[float]]] = {}

    # ─── main entry point ──────────────────────────────────────

    async def route(
        self,
        user_message: str,
        *,
        user_id: str | None = None,
        thread_id: str | None = None,
    ) -> RouteResult:
        """Select up to ``top_k`` in-scope memos relevant to ``user_message``.

        Empty result is the common case — most turns won't trip any
        threshold. That's by design: routing has to be conservative
        because every injected memo costs Opus input tokens.
        """
        if not user_message or not user_message.strip():
            return RouteResult(selections=[], candidates=0,
                               above_threshold=0, dropped_for_budget=0)

        # 1. Scope pre-filter — free.
        candidates = self.store.list_in_scope(
            user_id=user_id, thread_id=thread_id,
        )
        if not candidates:
            return RouteResult(selections=[], candidates=0,
                               above_threshold=0, dropped_for_budget=0)

        # 2. Ensure every candidate has a fresh embedding for its
        # description. Recompute only when the description changed since
        # last cache hit. This makes the per-turn cost = 1 embed call for
        # the user message + (new/changed memos count) embed calls.
        await self._ensure_embeddings(candidates)

        # 3. Embed the user message.
        try:
            msg_vec = (await self._embed([user_message]))[0]
        except Exception as exc:  # noqa: BLE001 — router failure must not break the turn
            logger.warning("memo router embed failed: %s", exc)
            return RouteResult(selections=[], candidates=len(candidates),
                               above_threshold=0, dropped_for_budget=0)

        # 4. Cosine-rank candidates.
        scored: list[Selection] = []
        for memo in candidates:
            cached = self._cache.get(memo.name)
            if cached is None:
                continue  # embedding still missing — treat as "not in result"
            _desc, memo_vec = cached
            score = _cosine(msg_vec, memo_vec)
            if score >= self.threshold:
                scored.append(Selection(memo=memo, score=score))

        scored.sort(key=lambda s: s.score, reverse=True)
        above = len(scored)
        scored = scored[: self.top_k]

        # 5. Enforce the character budget — drop lowest-scoring memos
        # until the total fits. Most turns won't hit this; it exists so
        # a pathological set of many-paragraph memos can't blow the
        # context window on a single turn.
        dropped = 0
        total_chars = 0
        kept: list[Selection] = []
        for sel in scored:
            cost = len(sel.memo.body) + 200  # ~200 chars overhead per reminder wrapper
            if total_chars + cost > self.budget_chars:
                dropped += 1
                continue
            kept.append(sel)
            total_chars += cost

        return RouteResult(
            selections=kept,
            candidates=len(candidates),
            above_threshold=above,
            dropped_for_budget=dropped,
        )

    # ─── internals ─────────────────────────────────────────────

    async def _ensure_embeddings(self, candidates: list[MemoSpec]) -> None:
        """Compute embeddings for any candidate whose description changed.

        Batches all new/changed descriptions into a single embed call so
        the FastEmbed model is warm-loaded once.
        """
        to_embed: list[tuple[str, str]] = []  # (name, description)
        for memo in candidates:
            cached = self._cache.get(memo.name)
            if cached is not None and cached[0] == memo.description:
                continue
            to_embed.append((memo.name, memo.description))

        if not to_embed:
            return

        descs = [d for _, d in to_embed]
        try:
            vectors = await self._embed(descs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "memo router could not embed %d descriptions: %s",
                len(descs), exc,
            )
            return

        for (name, desc), vec in zip(to_embed, vectors):
            self._cache[name] = (desc, vec)


# ─── cosine helper ───────────────────────────────────────────────


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. We assume both vectors are nonzero — FastEmbed
    never produces zero vectors for non-empty input, so this is safe.
    """
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
