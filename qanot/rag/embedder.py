"""Embedding providers for RAG with fallback chain.

Auto-detects the best available provider from the user's existing config.
No extra API keys required — reuses whatever provider keys are already set up.

Priority chain: Gemini (free tier) → OpenAI → FTS-only mode (no embeddings).
Anthropic and Groq do not offer embedding APIs.

Fallback distinguishes:
- Soft failures (missing API key, unsupported provider) → skip, try next
- Hard failures (network error, rate limit during embed()) → raise
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class EmbedderSoftError(Exception):
    """Non-fatal error: provider unavailable, try next in chain."""


class EmbedderHardError(Exception):
    """Fatal error: network failure, rate limit — stop trying."""


class Embedder(ABC):
    """Abstract base for embedding providers."""

    dimensions: int
    provider_name: str = "unknown"

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts into vectors."""
        ...

    async def embed_single(self, text: str) -> list[float]:
        """Embed a single text string."""
        return (await self.embed([text]))[0]


class GeminiEmbedder(Embedder):
    """Google Gemini embedding via OpenAI-compatible endpoint (free tier)."""

    provider_name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-embedding-001", base_url: str | None = None):
        import openai

        self.client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or "https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        self.model = model
        self.dimensions = 768

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using Gemini's OpenAI-compatible endpoint."""
        if not texts:
            return []
        all_embeddings: list[list[float]] = [None] * len(texts)  # type: ignore[list-item]
        for i in range(0, len(texts), 100):
            batch = texts[i : i + 100]
            response = await self.client.embeddings.create(
                input=batch,
                model=self.model,
                dimensions=self.dimensions,
            )
            # Sort by index to guarantee order matches input
            for d in sorted(response.data, key=lambda d: d.index):
                all_embeddings[i + d.index] = d.embedding
            logger.debug("Gemini embedded batch %d-%d (%d texts)", i, i + len(batch), len(batch))
        return all_embeddings


class OpenAIEmbedder(Embedder):
    """OpenAI text-embedding-3-small (1536 dims, $0.02/MTok)."""

    provider_name = "openai"

    def __init__(self, api_key: str, model: str = "text-embedding-3-small", base_url: str | None = None):
        import openai

        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = openai.AsyncOpenAI(**kwargs)
        self.model = model
        # nomic-embed-text = 768, OpenAI text-embedding-3-small = 1536
        _MODEL_DIMS = {"nomic-embed-text": 768, "text-embedding-3-small": 1536, "text-embedding-3-large": 3072}
        self.dimensions = _MODEL_DIMS.get(model, 768 if base_url and "11434" in base_url else 1536)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts in batches of 100."""
        if not texts:
            return []
        all_embeddings: list[list[float]] = [None] * len(texts)  # type: ignore[list-item]
        for i in range(0, len(texts), 100):
            batch = texts[i : i + 100]
            response = await self.client.embeddings.create(
                input=batch,
                model=self.model,
            )
            for j, d in enumerate(response.data):
                all_embeddings[i + j] = d.embedding
            logger.debug("OpenAI embedded batch %d-%d (%d texts)", i, i + len(batch), len(batch))
        return all_embeddings


def _collect_provider_keys(config) -> dict[str, dict]:
    """Extract all available provider API keys from config."""
    providers: dict[str, dict] = {}

    for pc in getattr(config, "providers", []):
        if pc.provider not in providers:
            providers[pc.provider] = {
                "api_key": pc.api_key,
                "base_url": getattr(pc, "base_url", ""),
            }

    provider_type = getattr(config, "provider", "")
    if provider_type and provider_type not in providers:
        api_key = getattr(config, "api_key", "")
        if api_key:
            providers[provider_type] = {"api_key": api_key}

    return providers


def create_embedder(config) -> Embedder | None:
    """Auto-detect best available embedder with fallback chain.

    Chain: Gemini (free) → OpenAI → None (FTS-only mode).

    Soft failures (missing key, unsupported provider) skip to next.
    Returns None if no compatible provider found — RAG falls back to FTS-only.
    """
    providers = _collect_provider_keys(config)
    errors: list[str] = []

    # Priority 1: Gemini (free embedding tier)
    if "gemini" in providers:
        info = providers["gemini"]
        if info.get("api_key"):
            try:
                embedder = GeminiEmbedder(
                    api_key=info["api_key"],
                    base_url=info.get("base_url") or None,
                )
                logger.info("RAG embedder: using Gemini gemini-embedding-001 (free tier)")
                return embedder
            except Exception as e:
                errors.append(f"gemini: {e}")
                logger.warning("Gemini embedder init failed: %s — trying next", e)
        else:
            errors.append("gemini: empty API key")

    # Priority 2: OpenAI (skip Ollama — embedding causes VRAM swap, use FTS-only instead)
    if "openai" in providers:
        info = providers["openai"]
        if info.get("api_key"):
            base_url = info.get("base_url", "")
            is_ollama = "ollama" in info.get("api_key", "").lower() or "11434" in base_url
            if is_ollama:
                logger.info("Ollama detected — skipping embedding (FTS-only mode to avoid VRAM swap)")
                return None
            try:
                model = "text-embedding-3-small"
                embedder = OpenAIEmbedder(
                    api_key=info["api_key"],
                    model=model,
                    base_url=base_url or None,
                )
                label = f"Ollama {model}" if is_ollama else f"OpenAI {model}"
                logger.info("RAG embedder: using %s", label)
                return embedder
            except Exception as e:
                errors.append(f"openai: {e}")
                logger.warning("OpenAI embedder init failed: %s — trying next", e)
        else:
            errors.append("openai: empty API key")

    # Fallback: FTS-only mode (no vector search)
    if errors:
        logger.warning(
            "RAG embedder: all providers failed (%s). Falling back to FTS-only mode.",
            "; ".join(errors),
        )
    else:
        logger.warning(
            "RAG embedder: no compatible provider found (available: %s). "
            "RAG will use FTS-only keyword search. "
            "Add a Gemini or OpenAI provider for vector search.",
            list(providers.keys()),
        )
    return None
