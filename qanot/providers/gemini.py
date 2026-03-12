"""Google Gemini provider — OpenAI-compatible API."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from qanot.providers.base import ProviderResponse, StreamEvent, ToolCall, Usage
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
    insert_idx = 0
    for i, msg in enumerate(messages):
        if msg.get("role") != "system":
            insert_idx = i
            break
    else:
        # All messages are system messages
        insert_idx = len(messages)

    # Check if the first non-system message is already a user message
    if insert_idx < len(messages) and messages[insert_idx].get("role") == "user":
        return messages

    synthetic = {"role": "user", "content": "(session start)"}
    return messages[:insert_idx] + [synthetic] + messages[insert_idx:]


def _sanitize_gemini_tools(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-style tools to OpenAI format and strip unsupported keys."""
    openai_tools = _anthropic_tools_to_openai(tools)
    return _strip_unsupported_keys(openai_tools)


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

    def _calc_cost(self, inp: int, out: int) -> float:
        prices = GEMINI_PRICING.get(self.model, {"input": 0.15, "output": 0.60})
        return inp * prices["input"] / 1_000_000 + out * prices["output"] / 1_000_000

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> ProviderResponse:
        converted = _ensure_user_first(_convert_messages(messages, system))

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
        }

        if tools:
            kwargs["tools"] = _sanitize_gemini_tools(tools)

        import openai as openai_mod

        try:
            response = await self.client.chat.completions.create(**kwargs)
        except openai_mod.APIError as e:
            logger.error("Gemini API error: %s", e)
            raise

        choice = response.choices[0]
        msg = choice.message

        text = msg.content or ""
        tool_calls: list[ToolCall] = []

        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    input=args,
                ))

        u = response.usage
        inp = u.prompt_tokens if u else 0
        out = u.completion_tokens if u else 0

        stop_reason = "tool_use" if tool_calls else "end_turn"

        return ProviderResponse(
            content=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=Usage(
                input_tokens=inp,
                output_tokens=out,
                cost=self._calc_cost(inp, out),
            ),
        )

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        converted = _ensure_user_first(_convert_messages(messages, system))

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if tools:
            kwargs["tools"] = _sanitize_gemini_tools(tools)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        partial_tools: dict[int, dict] = {}
        usage_data: dict | None = None

        import openai as openai_mod

        try:
            stream = await self.client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if chunk.usage:
                    usage_data = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    }

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                if delta.content:
                    text_parts.append(delta.content)
                    yield StreamEvent(type="text_delta", text=delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in partial_tools:
                            partial_tools[idx] = {
                                "id": tc_delta.id or "",
                                "name": "",
                                "arguments": "",
                            }
                        pt = partial_tools[idx]
                        if tc_delta.id:
                            pt["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                pt["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                pt["arguments"] += tc_delta.function.arguments

        except openai_mod.APIError as e:
            logger.error("Gemini streaming error: %s", e)
            raise

        for _idx, pt in sorted(partial_tools.items()):
            try:
                args = json.loads(pt["arguments"]) if pt["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            tc = ToolCall(id=pt["id"], name=pt["name"], input=args)
            tool_calls.append(tc)
            yield StreamEvent(type="tool_use", tool_call=tc)

        inp = usage_data["prompt_tokens"] if usage_data else 0
        out = usage_data["completion_tokens"] if usage_data else 0
        stop_reason = "tool_use" if tool_calls else "end_turn"

        response = ProviderResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=Usage(
                input_tokens=inp,
                output_tokens=out,
                cost=self._calc_cost(inp, out),
            ),
        )
        yield StreamEvent(type="done", response=response)
