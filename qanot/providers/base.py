"""Abstract LLM provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost: float = 0.0


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ProviderResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)


@dataclass
class StreamEvent:
    """A single event from a streaming LLM response."""

    type: str  # "text_delta", "tool_use", "done"
    text: str = ""
    tool_call: ToolCall | None = None
    response: ProviderResponse | None = None  # set on "done"


class LLMProvider(ABC):
    """Abstract base for LLM providers."""

    model: str

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> ProviderResponse:
        """Send messages to the LLM and get a response."""
        ...

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a response from the LLM.

        Default implementation falls back to non-streaming chat().
        Yields StreamEvent objects.
        """
        response = await self.chat(messages, tools, system)
        if response.content:
            yield StreamEvent(type="text_delta", text=response.content)
        for tc in response.tool_calls:
            yield StreamEvent(type="tool_use", tool_call=tc)
        yield StreamEvent(type="done", response=response)
