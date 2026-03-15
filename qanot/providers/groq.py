"""Groq provider — OpenAI-compatible API with ultra-fast inference."""

from __future__ import annotations

from qanot.providers.openai import OpenAIProvider

# Fallback pricing for unrecognised Groq models (same tier as llama-3.3-70b / groq/compound).
_DEFAULT_PRICING = {"input": 0.59, "output": 0.79}

# Groq pricing per million tokens (March 2026)
GROQ_PRICING = {
    # Llama 4 series
    "meta-llama/llama-4-scout-17b-16e-instruct": {"input": 0.11, "output": 0.18},
    # Llama 3.x series
    "llama-3.3-70b-versatile": _DEFAULT_PRICING,
    "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
    # Qwen / Kimi / Compound
    "qwen/qwen3-32b": {"input": 0.29, "output": 0.39},
    "moonshotai/kimi-k2-instruct": {"input": 0.20, "output": 0.20},
    "groq/compound": _DEFAULT_PRICING,
    "groq/compound-mini": {"input": 0.05, "output": 0.08},
}


class GroqProvider(OpenAIProvider):
    """Groq provider — uses OpenAI-compatible API with Groq's base URL.

    Inherits all OpenAI logic (chat, chat_stream, message conversion).
    Only overrides: base_url and pricing.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        base_url: str = "https://api.groq.com/openai/v1",
    ):
        if not isinstance(api_key, str) or not (api_key := api_key.strip()):
            raise ValueError("GroqProvider requires a non-empty api_key")
        if not isinstance(model, str) or not (model := model.strip()):
            raise ValueError("GroqProvider requires a non-empty model name")
        if len(model) > 256:
            raise ValueError(
                f"GroqProvider model name too long ({len(model)} chars), max 256"
            )
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise ValueError(
                f"GroqProvider base_url must use HTTPS to protect API credentials, got: {base_url!r}"
            )
        import openai
        self.client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def _calc_cost(self, input_tokens: int, output_tokens: int) -> float:
        prices = GROQ_PRICING.get(self.model, _DEFAULT_PRICING)
        return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
