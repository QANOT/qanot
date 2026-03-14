"""Failover provider — wraps multiple providers with automatic switching."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from qanot.providers.base import LLMProvider, ProviderResponse, StreamEvent
from qanot.providers.errors import (
    classify_error,
    PERMANENT_FAILURES,
    TRANSIENT_FAILURES,
)

logger = logging.getLogger(__name__)

# Cooldown period for failed providers (seconds)
COOLDOWN_SECONDS = 120


@dataclass
class ProviderProfile:
    """A single provider configuration."""
    name: str
    provider_type: str  # "anthropic", "openai", "gemini", "groq"
    api_key: str
    model: str
    base_url: str | None = None
    # Extended thinking (Anthropic only)
    thinking_level: str = "off"
    thinking_budget: int = 10000
    # Runtime state
    _cooldown_until: float = field(default=0.0, repr=False)
    _failure_count: int = field(default=0, repr=False)
    _last_error_type: str = field(default="", repr=False)

    @property
    def is_available(self) -> bool:
        """Check if this profile is currently available (not in cooldown)."""
        if self._last_error_type in PERMANENT_FAILURES:
            return False
        return time.monotonic() >= self._cooldown_until

    def mark_failed(self, error_type: str) -> None:
        """Mark this profile as failed with cooldown."""
        self._failure_count += 1
        self._last_error_type = error_type
        if error_type in PERMANENT_FAILURES:
            self._cooldown_until = float("inf")
            logger.warning("Provider %s permanently disabled: %s", self.name, error_type)
        else:
            cooldown = min(COOLDOWN_SECONDS * self._failure_count, 600)
            self._cooldown_until = time.monotonic() + cooldown
            logger.warning("Provider %s cooldown %ds: %s", self.name, cooldown, error_type)

        # Thinking level downgrade: if thinking is on and we're failing, try lower
        if self.thinking_level != "off" and self._failure_count >= 2:
            _THINKING_DOWNGRADE = {"high": "medium", "medium": "low", "low": "off"}
            new_level = _THINKING_DOWNGRADE.get(self.thinking_level)
            if new_level:
                logger.info("Downgrading thinking: %s → %s for %s",
                           self.thinking_level, new_level, self.name)
                self.thinking_level = new_level

    def mark_success(self) -> None:
        """Reset failure state on success."""
        self._failure_count = 0
        self._last_error_type = ""
        self._cooldown_until = 0.0


def _create_single_provider(profile: ProviderProfile) -> LLMProvider:
    """Create a concrete LLM provider from a profile."""
    if profile.provider_type == "anthropic":
        from qanot.providers.anthropic import AnthropicProvider
        return AnthropicProvider(
            api_key=profile.api_key,
            model=profile.model,
            thinking_level=profile.thinking_level,
            thinking_budget=profile.thinking_budget,
        )
    elif profile.provider_type == "openai":
        from qanot.providers.openai import OpenAIProvider
        kwargs = {"api_key": profile.api_key, "model": profile.model}
        if profile.base_url:
            kwargs["base_url"] = profile.base_url
        return OpenAIProvider(**kwargs)
    elif profile.provider_type == "groq":
        from qanot.providers.groq import GroqProvider
        return GroqProvider(api_key=profile.api_key, model=profile.model)
    elif profile.provider_type == "gemini":
        from qanot.providers.gemini import GeminiProvider
        kwargs = {"api_key": profile.api_key, "model": profile.model}
        if profile.base_url:
            kwargs["base_url"] = profile.base_url
        return GeminiProvider(**kwargs)
    else:
        raise ValueError(f"Unknown provider type: {profile.provider_type}")


class FailoverProvider(LLMProvider):
    """Provider that automatically fails over between multiple providers.

    Usage:
        profiles = [
            ProviderProfile(name="claude-main", provider_type="anthropic", ...),
            ProviderProfile(name="gemini-backup", provider_type="gemini", ...),
        ]
        provider = FailoverProvider(profiles)

    On rate_limit/auth/overloaded errors, automatically tries the next provider.
    Tracks cooldowns per-profile to avoid hammering failed providers.
    """

    def __init__(self, profiles: list[ProviderProfile]):
        if not profiles:
            raise ValueError("At least one provider profile required")
        self.profiles = profiles
        self._providers: dict[str, LLMProvider] = {}
        self._active_index = 0
        # Initialize first provider
        self._ensure_provider(0)
        self.model = profiles[0].model

    def _ensure_provider(self, index: int) -> LLMProvider:
        """Lazily create provider instances."""
        profile = self.profiles[index]
        if profile.name not in self._providers:
            self._providers[profile.name] = _create_single_provider(profile)
            logger.info("Initialized provider: %s (%s/%s)",
                       profile.name, profile.provider_type, profile.model)
        return self._providers[profile.name]

    def _get_available_indices(self) -> list[int]:
        """Get indices of available (non-cooled-down) profiles."""
        return [i for i, p in enumerate(self.profiles) if p.is_available]

    @property
    def active_profile(self) -> ProviderProfile:
        return self.profiles[self._active_index]

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> ProviderResponse:
        """Call chat with automatic failover."""
        available = self._get_available_indices()
        if not available:
            # All in cooldown — try first one anyway (least bad option)
            available = [0]
            logger.warning("All providers in cooldown, forcing first provider")

        # Try active provider first, then others
        order = []
        if self._active_index in available:
            order.append(self._active_index)
        for i in available:
            if i not in order:
                order.append(i)

        last_error: Exception | None = None
        for idx in order:
            profile = self.profiles[idx]
            provider = self._ensure_provider(idx)
            try:
                response = await provider.chat(messages, tools, system)
                # Success — update state
                profile.mark_success()
                self._active_index = idx
                self.model = profile.model
                return response
            except Exception as e:
                error_type = classify_error(e)
                logger.warning(
                    "Provider %s failed: %s [%s], trying next...",
                    profile.name, e, error_type,
                )
                profile.mark_failed(error_type)
                last_error = e

                # Don't try more providers for unknown errors
                if error_type == "unknown":
                    raise

        raise last_error or RuntimeError("No providers available")

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream with automatic failover."""
        available = self._get_available_indices()
        if not available:
            available = [0]

        order = []
        if self._active_index in available:
            order.append(self._active_index)
        for i in available:
            if i not in order:
                order.append(i)

        last_error: Exception | None = None
        for idx in order:
            profile = self.profiles[idx]
            provider = self._ensure_provider(idx)
            try:
                async for event in provider.chat_stream(messages, tools, system):
                    yield event
                # Success
                profile.mark_success()
                self._active_index = idx
                self.model = profile.model
                return
            except Exception as e:
                error_type = classify_error(e)
                logger.warning(
                    "Stream provider %s failed: %s [%s]",
                    profile.name, e, error_type,
                )
                profile.mark_failed(error_type)
                last_error = e
                if error_type == "unknown":
                    raise

        raise last_error or RuntimeError("No providers available")

    def status(self) -> list[dict]:
        """Get status of all provider profiles."""
        return [
            {
                "name": p.name,
                "type": p.provider_type,
                "model": p.model,
                "available": p.is_available,
                "failure_count": p._failure_count,
                "last_error": p._last_error_type,
                "active": i == self._active_index,
            }
            for i, p in enumerate(self.profiles)
        ]
