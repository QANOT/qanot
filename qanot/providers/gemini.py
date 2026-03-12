"""Google Gemini provider — OpenAI-compatible API."""

from __future__ import annotations

from qanot.providers.openai import OpenAIProvider

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


class GeminiProvider(OpenAIProvider):
    """Google Gemini provider via OpenAI-compatible API.

    Gemini supports the OpenAI chat completions format through
    generativelanguage.googleapis.com, so we reuse OpenAIProvider
    and only override base_url and pricing.
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

    def _calc_cost(self, inp: int, out: int) -> float:
        prices = GEMINI_PRICING.get(self.model, {"input": 0.15, "output": 0.60})
        return inp * prices["input"] / 1_000_000 + out * prices["output"] / 1_000_000
