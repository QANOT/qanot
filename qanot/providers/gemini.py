"""Google Gemini provider — OpenAI-compatible API."""

from __future__ import annotations

import logging
from typing import Any

from qanot.providers.openai import OpenAIProvider, _anthropic_tools_to_openai, _convert_messages

logger = logging.getLogger(__name__)

# Gemini pricing per million tokens (March 2026)
GEMINI_PRICING = {
    # Gemini 3.x series (latest)
    "gemini-3.1-pro-preview": {"input": 2.0, "output": 12.0},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-3-flash-preview": {"input": 0.15, "output": 0.60},
    # Gemini 2.x series (stable)
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
}

# JSON Schema keywords that Gemini does not support in tool definitions.
_UNSUPPORTED_SCHEMA_KEYS = frozenset({"patternProperties", "additionalProperties", "$ref"})


def _strip_unsupported_keys(obj: Any) -> Any:
    """Recursively remove unsupported JSON Schema keywords from a structure."""
    if isinstance(obj, dict):
        return {
            k: _strip_unsupported_keys(v)
            for k, v in obj.items()
            if k not in _UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(obj, list):
        return [_strip_unsupported_keys(item) for item in obj]
    return obj


def _ensure_user_first(messages: list[dict]) -> list[dict]:
    """Prepend a synthetic user turn if the first message is not from a user.

    Gemini requires the conversation to start with a user message.
    If the first message is a system message followed by a non-user message,
    or if there are no user messages at the start, a synthetic user turn is
    inserted right after any system messages.
    """
    if not messages:
        return messages

    # Find the index of the first non-system message
    insert_idx = next(
        (i for i, msg in enumerate(messages) if msg.get("role") != "system"),
        len(messages),
    )

    # Check if the first non-system message is already a user message
    if insert_idx < len(messages) and messages[insert_idx].get("role") == "user":
        return messages

    synthetic = {"role": "user", "content": "(session start)"}
    return messages[:insert_idx] + [synthetic] + messages[insert_idx:]


class GeminiProvider(OpenAIProvider):
    """Google Gemini provider via OpenAI-compatible API.

    Gemini supports the OpenAI chat completions format through
    generativelanguage.googleapis.com, so we reuse OpenAIProvider
    and only override base_url, pricing, and message/tool conversion.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/",
    ):
        import openai
        self.client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        prices = GEMINI_PRICING.get(model, GEMINI_PRICING["gemini-2.5-flash"])
        self._pricing = (prices["input"], prices["output"])

    def _calc_cost(self, inp: int, out: int) -> float:
        return (inp * self._pricing[0] + out * self._pricing[1]) / 1_000_000

    def _prepare_messages(self, messages: list[dict], system: str | None) -> list[dict]:
        """Convert messages and ensure conversation starts with a user turn."""
        return _ensure_user_first(_convert_messages(messages, system))

    def _prepare_tools(self, tools: list[dict]) -> list[dict]:
        """Convert tools and strip Gemini-unsupported JSON Schema keys."""
        return _strip_unsupported_keys(_anthropic_tools_to_openai(tools))
