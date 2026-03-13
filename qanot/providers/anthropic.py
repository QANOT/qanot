"""Anthropic Claude provider with streaming and prompt caching."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from qanot.providers.base import LLMProvider, ProviderResponse, StreamEvent, ToolCall, Usage

logger = logging.getLogger(__name__)

# Pricing per million tokens (as of 2025)
PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
}
DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}


def _is_oauth_token(api_key: str) -> bool:
    """Check if the API key is an Anthropic OAuth token."""
    return "sk-ant-oat" in api_key


class AnthropicProvider(LLMProvider):
    """Claude provider using the Anthropic API."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-6",
        thinking_level: str = "off",
        thinking_budget: int = 10000,
    ):
        self._is_oauth = _is_oauth_token(api_key)
        client_kwargs: dict[str, Any] = {}
        if self._is_oauth:
            # OAuth tokens use Bearer auth (auth_token), not x-api-key
            client_kwargs["auth_token"] = api_key
            client_kwargs["default_headers"] = {
                "anthropic-beta": "oauth-2025-04-20",
            }
            logger.info("Using Anthropic OAuth token — Bearer auth + beta headers enabled")
        else:
            client_kwargs["api_key"] = api_key
        self.client = anthropic.AsyncAnthropic(**client_kwargs)
        self.model = model
        self._thinking_level = thinking_level
        self._thinking_budget = thinking_budget

    @staticmethod
    def _extract_usage_dict(u) -> dict:
        """Extract usage dict from Anthropic response usage object."""
        return {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }

    @property
    def _thinking_enabled(self) -> bool:
        return self._thinking_level != "off"

    def _apply_thinking_kwargs(self, kwargs: dict[str, Any]) -> None:
        """Add extended thinking parameters to API kwargs if enabled."""
        if not self._thinking_enabled:
            return
        kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": self._thinking_budget,
        }
        # Anthropic requires temperature=1 when thinking is enabled
        kwargs["temperature"] = 1
        # Increase max_tokens to accommodate thinking budget
        kwargs["max_tokens"] = self._thinking_budget + 8192

    def _calc_cost(self, usage: dict) -> float:
        prices = PRICING.get(self.model, DEFAULT_PRICING)
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        cr = usage.get("cache_read_input_tokens", 0)
        cw = usage.get("cache_creation_input_tokens", 0)
        non_cached_input = max(0, inp - cr - cw)
        return (
            non_cached_input * prices["input"] / 1_000_000
            + out * prices["output"] / 1_000_000
            + cr * prices["cache_read"] / 1_000_000
            + cw * prices["cache_write"] / 1_000_000
        )

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> ProviderResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": messages,
        }

        if system:
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        if tools:
            kwargs["tools"] = tools

        self._apply_thinking_kwargs(kwargs)

        try:
            response = await self.client.messages.create(**kwargs)
        except anthropic.APIError as e:
            logger.error("Anthropic API error: %s", e)
            raise

        # Extract content — skip thinking blocks (they are internal reasoning)
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "thinking":
                # Skip thinking blocks — model used them internally
                continue
            elif block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    input=block.input,
                ))

        # Extract usage
        usage_dict = self._extract_usage_dict(response.usage)

        usage = Usage(
            input_tokens=usage_dict["input_tokens"],
            output_tokens=usage_dict["output_tokens"],
            cache_read_input_tokens=usage_dict["cache_read_input_tokens"],
            cache_creation_input_tokens=usage_dict["cache_creation_input_tokens"],
            cost=self._calc_cost(usage_dict),
        )

        return ProviderResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            usage=usage,
        )

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": messages,
        }

        if system:
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        if tools:
            kwargs["tools"] = tools

        self._apply_thinking_kwargs(kwargs)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        # Track partial tool_use blocks being built
        current_tool_id = ""
        current_tool_name = ""
        current_tool_json = ""
        # Track whether current block is a thinking block (skip its deltas)
        _in_thinking_block = False

        try:
            async with self.client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "thinking":
                            _in_thinking_block = True
                        elif block.type == "tool_use":
                            _in_thinking_block = False
                            current_tool_id = block.id
                            current_tool_name = block.name
                            current_tool_json = ""
                        else:
                            _in_thinking_block = False

                    elif event.type == "content_block_delta":
                        if _in_thinking_block:
                            # Skip thinking deltas — don't yield to user
                            continue
                        delta = event.delta
                        if delta.type == "text_delta":
                            text_parts.append(delta.text)
                            yield StreamEvent(type="text_delta", text=delta.text)
                        elif delta.type == "input_json_delta":
                            current_tool_json += delta.partial_json

                    elif event.type == "content_block_stop":
                        if _in_thinking_block:
                            _in_thinking_block = False
                            continue
                        if current_tool_id:
                            try:
                                tool_input = json.loads(current_tool_json) if current_tool_json else {}
                            except json.JSONDecodeError:
                                tool_input = {}
                            tc = ToolCall(
                                id=current_tool_id,
                                name=current_tool_name,
                                input=tool_input,
                            )
                            tool_calls.append(tc)
                            yield StreamEvent(type="tool_use", tool_call=tc)
                            current_tool_id = ""
                            current_tool_name = ""
                            current_tool_json = ""

                # Get final message for usage stats
                final = await stream.get_final_message()

        except anthropic.APIError as e:
            logger.error("Anthropic streaming error: %s", e)
            raise

        usage_dict = self._extract_usage_dict(final.usage)

        response = ProviderResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=final.stop_reason or "end_turn",
            usage=Usage(
                input_tokens=usage_dict["input_tokens"],
                output_tokens=usage_dict["output_tokens"],
                cache_read_input_tokens=usage_dict["cache_read_input_tokens"],
                cache_creation_input_tokens=usage_dict["cache_creation_input_tokens"],
                cost=self._calc_cost(usage_dict),
            ),
        )
        yield StreamEvent(type="done", response=response)
