"""Provider construction and wrapping (failover, multi-provider, routing)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qanot.config import Config
    from qanot.providers.base import LLMProvider


def find_gemini_key(config: Config) -> str | None:
    """Find a Gemini API key from config (multi-provider or dedicated field)."""
    # Check multi-provider configs
    for pc in config.providers:
        if pc.provider == "gemini" and pc.api_key:
            return pc.api_key
    # Check dedicated image_api_key
    if config.image_api_key:
        return config.image_api_key
    return None


def _anthropic_thinking_kwargs(provider_type: str, config: Config) -> dict:
    """Return thinking keyword arguments for Anthropic providers; empty dict otherwise."""
    if provider_type == "anthropic":
        return {
            "thinking_level": config.thinking_level,
            "thinking_budget": config.thinking_budget,
            "code_execution": config.code_execution,
            "memory_tool": config.memory_tool,
            "context_editing": config.context_editing_enabled,
            "context_editing_trigger_tokens": config.context_editing_trigger_tokens,
            "context_editing_keep_tool_uses": config.context_editing_keep_tool_uses,
            "context_editing_clear_at_least_tokens": config.context_editing_clear_at_least_tokens,
        }
    return {}


def _create_provider(config: Config, logger: logging.Logger) -> LLMProvider:
    """Create LLM provider based on config.

    Supports two config formats:
    1. Single provider: { "provider": "anthropic", "model": "...", "api_key": "..." }
    2. Multi-provider: { "providers": [{ "name": "main", "provider": "anthropic", ... }, ...] }

    When multiple providers are configured, creates a FailoverProvider that
    automatically switches between them on errors.
    """
    from qanot.providers.failover import FailoverProvider, ProviderProfile, _create_single_provider

    # Multi-provider mode
    if config.providers:
        profiles = [
            ProviderProfile(
                name=pc.name,
                provider_type=pc.provider,
                api_key=pc.api_key,
                model=pc.model,
                base_url=pc.base_url or None,
                **_anthropic_thinking_kwargs(pc.provider, config),
            )
            for pc in config.providers
        ]
        provider = FailoverProvider(profiles)
        logger.info("Multi-provider mode: %s (failover enabled)", ", ".join(p.name for p in profiles))
        return provider

    # Single provider mode — reuse the same factory
    profile = ProviderProfile(
        name="default",
        provider_type=config.provider,
        api_key=config.api_key,
        model=config.model,
        **_anthropic_thinking_kwargs(config.provider, config),
    )
    return _create_single_provider(profile)


def build_provider(config: Config, logger: logging.Logger) -> LLMProvider:
    """Build the full provider stack: base provider + optional routing wrapper.

    Returns the outermost provider (RoutingProvider when ``routing_enabled``,
    otherwise the bare provider from ``_create_provider``).
    """
    provider = _create_provider(config, logger)
    logger.info("Provider initialized: %s", config.provider)

    # Wrap with routing provider if enabled (cost optimization)
    if config.routing_enabled:
        from qanot.routing import RoutingProvider
        routing_mid_model = getattr(config, "routing_mid_model", "claude-sonnet-4-6")
        provider = RoutingProvider(
            provider=provider,
            cheap_model=config.routing_model,
            mid_model=routing_mid_model,
            threshold=config.routing_threshold,
        )
        logger.info(
            "3-tier routing: simple → %s, moderate → %s, complex → %s",
            config.routing_model, routing_mid_model, config.model,
        )

    return provider
