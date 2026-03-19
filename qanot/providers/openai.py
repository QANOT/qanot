"""OpenAI GPT provider with function calling."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import openai

from qanot.providers.base import LLMProvider, ProviderResponse, StreamEvent, ToolCall, Usage

logger = logging.getLogger(__name__)

PRICING = {
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}
DEFAULT_PRICING = {"input": 2.0, "output": 8.0}
_MAX_TOOL_JSON = 1_000_000  # 1 MB cap on accumulated tool arguments


def _anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-style tool definitions to OpenAI function calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


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
                parts: list[dict | str] = []
                has_images = False
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(block)
                        elif block.get("type") == "image":
                            # Convert Anthropic image block → OpenAI image_url
                            source = block.get("source", {})
                            if source.get("type") == "base64":
                                data_uri = f"data:{source['media_type']};base64,{source['data']}"
                                parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": data_uri},
                                })
                                has_images = True
                        elif block.get("type") == "tool_result":
                            result.append({
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", ""),
                                "content": _extract_text(block.get("content", "")),
                            })
                            continue
                if parts:
                    if has_images:
                        # Multi-modal content: keep as list of content parts
                        content_parts = [
                            {"type": "text", "text": p["text"]} if p.get("type") == "text" else p
                            for p in parts
                            if isinstance(p, dict)
                        ]
                        result.append({"role": "user", "content": content_parts})
                    else:
                        # Text-only: join as string
                        result.append({"role": "user", "content": "\n".join(
                            p["text"] for p in parts if isinstance(p, dict) and p.get("type") == "text"
                        )})

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

    def __init__(self, api_key: str, model: str = "gpt-4.1", base_url: str | None = None):
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = openai.AsyncOpenAI(**kwargs)
        self.model = model
        # Detect Ollama — disable thinking mode for Qwen models (30x faster)
        self._is_ollama = bool(base_url and "11434" in base_url)

    @property
    def _ollama_base(self) -> str:
        """Get Ollama native API base URL from OpenAI base_url."""
        base = str(self.client.base_url).rstrip("/")
        return base.replace("/v1", "")  # http://localhost:11434

    async def _ollama_chat(self, messages: list[dict], tools: list[dict] | None = None) -> ProviderResponse:
        """Call Ollama native API with think=false."""
        import httpx

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{self._ollama_base}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

        msg = data.get("message", {})
        text = msg.get("content", "")
        tool_calls_raw = msg.get("tool_calls", [])

        tc_list = []
        for i, tc in enumerate(tool_calls_raw):
            fn = tc.get("function", {})
            tc_list.append(ToolCall(
                id=tc.get("id", f"call_{i}"),
                name=fn.get("name", ""),
                input=fn.get("arguments", {}),
            ))

        inp = data.get("prompt_eval_count", 0)
        out = data.get("eval_count", 0)
        stop = "tool_use" if tc_list else "end_turn"

        return ProviderResponse(
            content=text,
            tool_calls=tc_list,
            stop_reason=stop,
            usage=Usage(input_tokens=inp, output_tokens=out, cost=0.0),
        )

    async def _ollama_chat_stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[StreamEvent]:
        """Stream from Ollama native API with think=false."""
        import httpx

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": False,
        }
        if tools:
            payload["tools"] = tools

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        inp = 0
        out = 0

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", f"{self._ollama_base}/api/chat", json=payload) as resp:
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg = data.get("message", {})
                    content = msg.get("content", "")
                    if content:
                        text_parts.append(content)
                        yield StreamEvent(type="text_delta", text=content)

                    # Tool calls come in the final message
                    if data.get("done"):
                        inp = data.get("prompt_eval_count", 0)
                        out = data.get("eval_count", 0)
                        for i, tc in enumerate(msg.get("tool_calls", [])):
                            fn = tc.get("function", {})
                            tool_calls.append(ToolCall(
                                id=tc.get("id", f"call_{i}"),
                                name=fn.get("name", ""),
                                input=fn.get("arguments", {}),
                            ))

        stop = "tool_use" if tool_calls else "end_turn"
        response = ProviderResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=Usage(input_tokens=inp, output_tokens=out, cost=0.0),
        )
        yield StreamEvent(type="done", response=response)

    def _calc_cost(self, input_tokens: int, output_tokens: int) -> float:
        prices = PRICING.get(self.model, DEFAULT_PRICING)
        return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000

    @staticmethod
    def _stop_reason(tool_calls: list) -> str:
        return "tool_use" if tool_calls else "end_turn"

    def _prepare_messages(self, messages: list[dict], system: str | None) -> list[dict]:
        """Convert messages to OpenAI format. Override in subclasses for custom preprocessing."""
        return _convert_messages(messages, system)

    def _prepare_tools(self, tools: list[dict]) -> list[dict]:
        """Convert tools to OpenAI format. Override in subclasses for custom preprocessing."""
        return _anthropic_tools_to_openai(tools)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> ProviderResponse:
        converted = self._prepare_messages(messages, system)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
        }

        if tools:
            kwargs["tools"] = self._prepare_tools(tools)

        # Ollama: use native API with think=false (OpenAI compat doesn't support it)
        if self._is_ollama:
            return await self._ollama_chat(converted, kwargs.get("tools"))

        try:
            response = await self.client.chat.completions.create(**kwargs)
        except openai.APIError as e:
            logger.error("OpenAI API error: %s", e)
            raise

        if not response.choices:
            raise ValueError(
                f"OpenAI returned empty choices for model {self.model}; "
                f"finish_reason may be content_filter or the request was rejected"
            )

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

        return ProviderResponse(
            content=text,
            tool_calls=tool_calls,
            stop_reason=self._stop_reason(tool_calls),
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
        converted = self._prepare_messages(messages, system)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if tools:
            kwargs["tools"] = self._prepare_tools(tools)

        # Ollama: use native API with think=false
        if self._is_ollama:
            async for event in self._ollama_chat_stream(converted, kwargs.get("tools")):
                yield event
            return

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        # Track partial tool calls: index -> {id, name, arguments}
        partial_tools: dict[int, dict] = {}
        usage_data: dict | None = None

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

                # Text content
                if delta.content:
                    text_parts.append(delta.content)
                    yield StreamEvent(type="text_delta", text=delta.content)

                # Tool call deltas
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
                                if len(pt["arguments"]) + len(tc_delta.function.arguments) > _MAX_TOOL_JSON:
                                    logger.warning(
                                        "Tool call arguments for index %d exceeded %d byte limit, truncating",
                                        idx, _MAX_TOOL_JSON,
                                    )
                                else:
                                    pt["arguments"] += tc_delta.function.arguments

        except openai.APIError as e:
            logger.error("OpenAI streaming error: %s", e)
            raise

        # Build final tool calls
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
        stop_reason = self._stop_reason(tool_calls)

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
