"""OpenAI GPT provider with function calling."""

from __future__ import annotations

import json
import logging
from typing import Any

import openai

from qanot.providers.base import LLMProvider, ProviderResponse, ToolCall, Usage

logger = logging.getLogger(__name__)

PRICING = {
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}
DEFAULT_PRICING = {"input": 2.0, "output": 8.0}


def _anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-style tool definitions to OpenAI function calling format."""
    result = []
    for t in tools:
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return result


def _convert_messages(messages: list[dict], system: str | None) -> list[dict]:
    """Convert Anthropic-style messages to OpenAI format."""
    result = []
    if system:
        result.append({"role": "system", "content": system})

    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")

        if role == "user":
            if isinstance(content, str):
                result.append({"role": "user", "content": content})
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(block["text"])
                        elif block.get("type") == "tool_result":
                            result.append({
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", ""),
                                "content": _extract_text(block.get("content", "")),
                            })
                            continue
                if parts:
                    result.append({"role": "user", "content": "\n".join(parts)})

        elif role == "assistant":
            if isinstance(content, str):
                result.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                text_parts = []
                tool_calls_list = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block["text"])
                        elif block.get("type") == "tool_use":
                            tool_calls_list.append({
                                "id": block["id"],
                                "type": "function",
                                "function": {
                                    "name": block["name"],
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            })
                msg_out: dict[str, Any] = {"role": "assistant"}
                if text_parts:
                    msg_out["content"] = "\n".join(text_parts)
                if tool_calls_list:
                    msg_out["tool_calls"] = tool_calls_list
                if "content" in msg_out or "tool_calls" in msg_out:
                    result.append(msg_out)

    return result


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content)


class OpenAIProvider(LLMProvider):
    """OpenAI GPT provider."""

    def __init__(self, api_key: str, model: str = "gpt-4.1"):
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model

    def _calc_cost(self, inp: int, out: int) -> float:
        prices = PRICING.get(self.model, DEFAULT_PRICING)
        return inp * prices["input"] / 1_000_000 + out * prices["output"] / 1_000_000

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> ProviderResponse:
        converted = _convert_messages(messages, system)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
        }

        if tools:
            kwargs["tools"] = _anthropic_tools_to_openai(tools)

        try:
            response = await self.client.chat.completions.create(**kwargs)
        except openai.APIError as e:
            logger.error("OpenAI API error: %s", e)
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
