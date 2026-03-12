"""Embedding providers for RAG.

Auto-detects the best available provider from the user's existing config.
No extra API keys required — reuses whatever provider keys are already set up.

Priority: Gemini (free tier) > OpenAI > fallback error.
Anthropic and Groq do not offer embedding APIs.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class Embedder(ABC):
    """Abstract base for embedding providers."""

    dimensions: int

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts into vectors."""
        ...

    async def embed_single(self, text: str) -> list[float]:
        """Embed a single text string."""
        return (await self.embed([text]))[0]


class GeminiEmbedder(Embedder):
    """Google Gemini text-embedding-004 (768 dims, generous free tier)."""

    def __init__(self, api_key: str, model: str = "text-embedding-004", base_url: str | None = None):
        import openai

        self.client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or "https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        self.model = model
        self.dimensions = 768

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using Gemini's OpenAI-compatible endpoint."""
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), 100):
            batch = texts[i : i + 100]
            response = await self.client.embeddings.create(
                input=batch,
                model=self.model,
            )
            all_embeddings.extend([d.embedding for d in response.data])
            logger.debug("Gemini embedded batch %d-%d (%d texts)", i, i + len(batch), len(batch))
        return all_embeddings


class OpenAIEmbedder(Embedder):
    """OpenAI text-embedding-3-small (1536 dims, $0.02/MTok)."""

    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        import openai

        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model
        self.dimensions = 1536

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts in batches of 100."""
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), 100):
            batch = texts[i : i + 100]
            response = await self.client.embeddings.create(
                input=batch,
                model=self.model,
            )
            all_embeddings.extend([d.embedding for d in response.data])
            logger.debug("OpenAI embedded batch %d-%d (%d texts)", i, i + len(batch), len(batch))
        return all_embeddings


def create_embedder(config) -> Embedder | None:
    """Auto-detect best available embedder from existing provider config.

    Priority: Gemini (free) > OpenAI > None.
    Anthropic and Groq don't offer embedding APIs.

    Args:
        config: Qanot Config object with provider/providers fields.

    Returns:
        Embedder instance or None if no compatible provider found.
    """
    # Collect all available provider keys by type
    providers: dict[str, dict] = {}

    # Check multi-provider configs first
    for pc in getattr(config, "providers", []):
        if pc.provider not in providers:
            providers[pc.provider] = {
                "api_key": pc.api_key,
                "base_url": getattr(pc, "base_url", ""),
            }

    # Check single-provider config
    provider_type = getattr(config, "provider", "")
    if provider_type and provider_type not in providers:
        api_key = getattr(config, "api_key", "")
        if api_key:
            providers[provider_type] = {"api_key": api_key}

    # Priority 1: Gemini (free embedding tier)
    if "gemini" in providers:
        info = providers["gemini"]
        logger.info("RAG embedder: using Gemini text-embedding-004 (free tier)")
        return GeminiEmbedder(
            api_key=info["api_key"],
            base_url=info.get("base_url") or None,
        )

    # Priority 2: OpenAI
    if "openai" in providers:
        info = providers["openai"]
        logger.info("RAG embedder: using OpenAI text-embedding-3-small")
        return OpenAIEmbedder(api_key=info["api_key"])

    # Groq uses OpenAI-compatible API but doesn't support embeddings
    # Anthropic doesn't have an embedding API
    logger.warning(
        "RAG embedder: no compatible provider found. "
        "Add a Gemini or OpenAI provider to enable RAG. "
        "Available providers: %s",
        list(providers.keys()),
    )
    return None
