"""Anthropic Claude provider with streaming and prompt caching."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from qanot.providers.base import LLMProvider, ProviderResponse, StreamEvent, ToolCall, Usage

logger = logging.getLogger(__name__)

# Pricing per million tokens (as of 2025)
PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
}
DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}

# Idle watchdog: abort stream if no events arrive within this window (seconds)
STREAM_IDLE_TIMEOUT = 60

# Server-side code execution tool definition
CODE_EXECUTION_TOOL = {"type": "code_execution_20250825", "name": "code_execution"}

# Anthropic memory tool type hint — makes Claude use trained memory behavior
# (auto-check /memories on startup, save progress, structured note-taking)
MEMORY_TOOL_TYPE = {"type": "memory_20250818", "name": "memory"}

# Block types produced by server-side code execution (skip in our tool_use handling)
_SERVER_TOOL_TYPES = frozenset({
    "server_tool_use",
    "bash_code_execution_tool_result",
    "text_editor_code_execution_tool_result",
})

# Regex to parse context overflow: "… 190000 + 8192 > 200000"
_OVERFLOW_RE = re.compile(r"(\d+)\s*\+\s*(\d+)\s*>\s*(\d+)")

# Floor for max_tokens when retrying after overflow
_MIN_MAX_TOKENS = 1024


def _parse_overflow(error_msg: str) -> tuple[int, int, int] | None:
    """Parse context overflow error. Returns (input_tokens, max_tokens, context_limit) or None."""
    m = _OVERFLOW_RE.search(error_msg)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


async def _iter_with_timeout(stream_iter: Any, timeout: float) -> AsyncIterator:
    """Wrap async iterator with per-event idle timeout."""
    aiter = stream_iter.__aiter__()
    while True:
        try:
            event = await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
            yield event
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            logger.warning("Stream idle for %ss — no events received, aborting", timeout)
            raise TimeoutError(f"Stream idle for {timeout}s — no events received")


def _is_oauth_token(api_key: str) -> bool:
    """Check if the API key is an Anthropic OAuth token."""
    return api_key.startswith("sk-ant-oat")


def _extract_code_execution_text(block) -> str | None:
    """Extract human-readable text from a server-side code execution result block."""
    btype = getattr(block, "type", "")
    if btype == "server_tool_use":
        name = getattr(block, "name", "")
        inp = getattr(block, "input", {})
        if name == "bash_code_execution" and isinstance(inp, dict):
            cmd = inp.get("command", "")
            return f"```bash\n$ {cmd}\n```" if cmd else None
        if name == "text_editor_code_execution" and isinstance(inp, dict):
            op = inp.get("command", "")
            path = inp.get("path", "")
            return f"[file {op}: {path}]" if path else None
        return None

    content = getattr(block, "content", None)
    if content is None:
        return None

    if btype == "bash_code_execution_tool_result":
        ct = getattr(content, "type", "")
        if ct == "bash_code_execution_result":
            stdout = getattr(content, "stdout", "") or ""
            stderr = getattr(content, "stderr", "") or ""
            rc = getattr(content, "return_code", 0)
            parts = []
            if stdout:
                parts.append(stdout.rstrip())
            if stderr:
                parts.append(f"stderr: {stderr.rstrip()}")
            if rc != 0:
                parts.append(f"(exit code {rc})")
            return "\n".join(parts) if parts else None
        if "error" in ct:
            code = getattr(content, "error_code", "unknown")
            return f"[code execution error: {code}]"

    if btype == "text_editor_code_execution_tool_result":
        ct = getattr(content, "type", "")
        if ct == "text_editor_code_execution_result":
            file_content = getattr(content, "content", None)
            if file_content:
                return file_content
            lines = getattr(content, "lines", None)
            if lines:
                return "\n".join(lines)
            return "[file operation completed]"

    return None


class AnthropicProvider(LLMProvider):
    """Claude provider using the Anthropic API."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-6",
        thinking_level: str = "off",
        thinking_budget: int = 10000,
        code_execution: bool = False,
        memory_tool: bool = False,
        context_editing: bool = False,
        context_editing_trigger_tokens: int = 30000,
        context_editing_keep_tool_uses: int = 3,
        context_editing_clear_at_least_tokens: int = 5000,
    ):
        self._is_oauth = _is_oauth_token(api_key)
        # Base beta headers — context editing is opt-in because the beta
        # header appearing in EVERY request invalidates Anthropic's prefix
        # cache for pre-existing OAuth users the first time it appears.
        beta_headers = [
            "claude-code-20250219",
            "oauth-2025-04-20",
            "fine-grained-tool-streaming-2025-05-14",
        ]
        if context_editing:
            beta_headers.append("context-management-2025-06-27")
        client_kwargs: dict[str, Any] = {}
        if self._is_oauth:
            # OAuth tokens: Claude Code identity required for Opus/Sonnet access
            client_kwargs["api_key"] = None
            client_kwargs["auth_token"] = api_key
            client_kwargs["default_headers"] = {
                "anthropic-dangerous-direct-browser-access": "true",
                "anthropic-beta": ",".join(beta_headers),
                "user-agent": "claude-cli/1.0.0",
                "x-app": "cli",
            }
            logger.info("Using Anthropic OAuth token — Claude Code identity headers enabled")
        else:
            client_kwargs["api_key"] = api_key
        self.client = anthropic.AsyncAnthropic(**client_kwargs)
        self.model = model
        self._thinking_level = thinking_level
        self._thinking_budget = thinking_budget
        self._code_execution = code_execution
        self._memory_tool = memory_tool
        self._context_editing = context_editing
        self._context_editing_cfg = {
            "trigger_tokens": context_editing_trigger_tokens,
            "keep_tool_uses": context_editing_keep_tool_uses,
            "clear_at_least_tokens": context_editing_clear_at_least_tokens,
        }
        if context_editing:
            logger.info(
                "Context editing enabled (trigger=%d, keep=%d, clear_at_least=%d)",
                context_editing_trigger_tokens,
                context_editing_keep_tool_uses,
                context_editing_clear_at_least_tokens,
            )
        # Container ID for cross-turn state persistence
        self._container_id: str | None = None

    @staticmethod
    def _extract_usage_dict(u) -> dict:
        """Extract usage dict from Anthropic response usage object."""
        return {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }

    def set_thinking(self, level: str, budget: int | None = None) -> None:
        """Update thinking level and budget at runtime (called by /think handler)."""
        self._thinking_level = level
        if budget is not None:
            self._thinking_budget = budget

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
            # Skip streaming thinking tokens — we don't display them.
            # Reduces time-to-first-text-token. Cost stays the same.
            "display": "omitted",
        }
        # Anthropic requires temperature=1 when thinking is enabled
        kwargs["temperature"] = 1
        # Increase max_tokens to accommodate thinking budget
        kwargs["max_tokens"] = self._thinking_budget + 8192

    def _inject_context_editing(self, kwargs: dict[str, Any]) -> None:
        """Attach the context_management spec + beta header when enabled.

        `context_management` is a beta-only top-level field on the Messages
        request, so we pass it via `extra_body` (the SDK merges it into the
        request JSON without validation).

        The beta header itself is already in default_headers for OAuth
        clients; API-key clients get it per-request via extra_headers.
        """
        if not self._context_editing:
            return
        cfg = self._context_editing_cfg
        context_mgmt = {
            "edits": [
                {
                    "type": "clear_tool_uses_20250919",
                    "trigger": {"type": "input_tokens", "value": cfg["trigger_tokens"]},
                    "keep": {"type": "tool_uses", "value": cfg["keep_tool_uses"]},
                    "clear_at_least": {
                        "type": "input_tokens",
                        "value": cfg["clear_at_least_tokens"],
                    },
                }
            ]
        }
        extra_body = dict(kwargs.get("extra_body") or {})
        extra_body["context_management"] = context_mgmt
        kwargs["extra_body"] = extra_body

        if not self._is_oauth:
            # API-key path needs the beta header per-request.
            extra = dict(kwargs.get("extra_headers") or {})
            existing = extra.get("anthropic-beta", "")
            if "context-management-2025-06-27" not in existing:
                extra["anthropic-beta"] = (
                    f"{existing},context-management-2025-06-27"
                    if existing else "context-management-2025-06-27"
                )
            kwargs["extra_headers"] = extra

    def _inject_server_tools(self, kwargs: dict[str, Any]) -> None:
        """Inject Anthropic server-side tools (code execution, memory type hint)."""
        tools = kwargs.get("tools") or []
        changed = False

        # Code execution (server-side sandbox)
        if self._code_execution:
            if not any(t.get("type") == "code_execution_20250825" for t in tools):
                tools.append(CODE_EXECUTION_TOOL)
                changed = True
            if self._container_id:
                kwargs["container"] = self._container_id

        # Memory tool type hint — replaces our client-side "memory" tool def
        # with Anthropic's typed version (only type + name, no extra fields)
        if self._memory_tool:
            for i, t in enumerate(tools):
                if t.get("name") == "memory" and t.get("type") != "memory_20250818":
                    tools[i] = dict(MEMORY_TOOL_TYPE)
                    changed = True
                    break
            else:
                if not any(t.get("type") == "memory_20250818" for t in tools):
                    tools.append(dict(MEMORY_TOOL_TYPE))
                    changed = True

        if changed:
            kwargs["tools"] = tools

    def _capture_container(self, response) -> None:
        """Capture container ID from response for cross-turn reuse."""
        container = getattr(response, "container", None)
        if container:
            cid = getattr(container, "id", None)
            if cid:
                self._container_id = cid

    def _build_usage(self, usage_dict: dict) -> Usage:
        """Construct a Usage object from a raw usage dict."""
        return Usage(
            input_tokens=usage_dict["input_tokens"],
            output_tokens=usage_dict["output_tokens"],
            cache_read_input_tokens=usage_dict["cache_read_input_tokens"],
            cache_creation_input_tokens=usage_dict["cache_creation_input_tokens"],
            cost=self._calc_cost(usage_dict),
        )

    def _calc_cost(self, usage: dict) -> float:
        prices = PRICING.get(self.model, DEFAULT_PRICING)
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        cr = usage.get("cache_read_input_tokens", 0)
        cw = usage.get("cache_creation_input_tokens", 0)
        return (
            inp * prices["input"]
            + out * prices["output"]
            + cr * prices["cache_read"]
            + cw * prices["cache_write"]
        ) / 1_000_000

    def _build_system_blocks(self, system: str) -> list[dict]:
        """Build the system prompt block list with cache-optimized splitting.

        Splits system prompt on CACHE_BOUNDARY marker:
        - Static prefix (SOUL, IDENTITY, AGENTS, TOOLS, plugins) → cache_control
        - Dynamic suffix (Session Info, context %) → no cache_control

        This maximizes Anthropic prompt cache hit rate by keeping the
        stable prefix identical across calls.
        """
        from qanot.prompt import _CACHE_BOUNDARY

        blocks: list[dict] = []

        # OAuth tokens MUST include Claude Code identity for Opus/Sonnet access
        if self._is_oauth:
            blocks.append({
                "type": "text",
                "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                "cache_control": {"type": "ephemeral"},
            })

        # Split on cache boundary marker
        if _CACHE_BOUNDARY in system:
            static, dynamic = system.split(_CACHE_BOUNDARY, 1)
            static = static.strip()
            dynamic = dynamic.strip()

            # Static part — cacheable (changes rarely)
            if static:
                blocks.append({
                    "type": "text",
                    "text": static,
                    "cache_control": {"type": "ephemeral"},
                })

            # Dynamic part — NOT cached (changes every request)
            if dynamic:
                blocks.append({
                    "type": "text",
                    "text": dynamic,
                })
        else:
            # Fallback: no boundary marker, cache entire prompt
            blocks.append({
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            })

        return blocks

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
            kwargs["system"] = self._build_system_blocks(system)

        if tools:
            kwargs["tools"] = tools

        self._apply_thinking_kwargs(kwargs)
        self._inject_server_tools(kwargs)
        self._inject_context_editing(kwargs)

        for _overflow_attempt in range(2):
            try:
                response = await self.client.messages.create(**kwargs)
                break
            except anthropic.APIError as e:
                parsed = _parse_overflow(str(e))
                if parsed and _overflow_attempt == 0:
                    input_tokens, _, context_limit = parsed
                    safety_buffer = 1000
                    new_max = max(_MIN_MAX_TOKENS, context_limit - input_tokens - safety_buffer)
                    logger.info(
                        "Context overflow: reducing max_tokens %d -> %d",
                        kwargs["max_tokens"], new_max,
                    )
                    kwargs["max_tokens"] = new_max
                    continue
                logger.error("Anthropic API error: %s", e)
                raise

        self._capture_container(response)

        # Extract content — skip thinking blocks, surface code execution results
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "thinking":
                continue
            elif block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    input=block.input,
                ))
            elif block.type in _SERVER_TOOL_TYPES:
                # Server-side code execution — surface results as text
                exec_text = _extract_code_execution_text(block)
                if exec_text:
                    text_parts.append(exec_text)

        return ProviderResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            usage=self._build_usage(self._extract_usage_dict(response.usage)),
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
            kwargs["system"] = self._build_system_blocks(system)

        if tools:
            kwargs["tools"] = tools

        self._apply_thinking_kwargs(kwargs)
        self._inject_server_tools(kwargs)
        self._inject_context_editing(kwargs)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        # Track partial tool_use blocks being built
        current_tool_id = ""
        current_tool_name = ""
        current_tool_json_parts: list[str] = []
        # Track whether current block is a thinking block (skip its deltas)
        _in_thinking_block = False
        # Track server-side tool blocks (code execution)
        _in_server_block = False

        # Retry once on context overflow with reduced max_tokens
        for _overflow_attempt in range(2):
            try:
                async with self.client.messages.stream(**kwargs) as stream:
                    async for event in _iter_with_timeout(stream, STREAM_IDLE_TIMEOUT):
                        if event.type == "content_block_start":
                            block = event.content_block
                            if block.type == "thinking":
                                _in_thinking_block = True
                                _in_server_block = False
                            elif block.type in _SERVER_TOOL_TYPES:
                                _in_server_block = True
                                _in_thinking_block = False
                                # Surface server tool invocations as text
                                exec_text = _extract_code_execution_text(block)
                                if exec_text:
                                    text_parts.append(exec_text)
                                    yield StreamEvent(type="text_delta", text=exec_text)
                            elif block.type == "tool_use":
                                _in_thinking_block = False
                                _in_server_block = False
                                current_tool_id = block.id
                                current_tool_name = block.name
                                current_tool_json_parts = []
                            else:
                                _in_thinking_block = False
                                _in_server_block = False

                        elif event.type == "content_block_delta":
                            if _in_thinking_block or _in_server_block:
                                continue
                            delta = event.delta
                            if delta.type == "text_delta":
                                text_parts.append(delta.text)
                                yield StreamEvent(type="text_delta", text=delta.text)
                            elif delta.type == "input_json_delta":
                                current_tool_json_parts.append(delta.partial_json)

                        elif event.type == "content_block_stop":
                            if _in_thinking_block:
                                _in_thinking_block = False
                                continue
                            if _in_server_block:
                                _in_server_block = False
                                continue
                            if current_tool_id:
                                current_tool_json = "".join(current_tool_json_parts)
                                # Guard against unbounded tool JSON accumulation
                                if len(current_tool_json) > 1_000_000:
                                    logger.warning(
                                        "Tool call %s JSON too large (%d bytes), truncating",
                                        current_tool_name, len(current_tool_json),
                                    )
                                    tool_input = {}
                                else:
                                    try:
                                        tool_input = json.loads(current_tool_json) if current_tool_json else {}
                                    except json.JSONDecodeError:
                                        logger.warning(
                                            "Invalid JSON in tool call %s: %s",
                                            current_tool_name, current_tool_json[:200],
                                        )
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
                                current_tool_json_parts = []

                    # Get final message for usage stats
                    final = await stream.get_final_message()
                break  # success, exit retry loop

            except anthropic.APIError as e:
                parsed = _parse_overflow(str(e))
                if parsed and _overflow_attempt == 0:
                    input_tokens, _, context_limit = parsed
                    safety_buffer = 1000
                    new_max = max(_MIN_MAX_TOKENS, context_limit - input_tokens - safety_buffer)
                    logger.info(
                        "Context overflow (stream): reducing max_tokens %d -> %d",
                        kwargs["max_tokens"], new_max,
                    )
                    kwargs["max_tokens"] = new_max
                    # Reset accumulation state for the retry.
                    # Overflow errors are upfront (before streaming), so these
                    # should already be empty — but clear defensively.
                    text_parts.clear()
                    tool_calls.clear()
                    current_tool_id = ""
                    current_tool_name = ""
                    current_tool_json_parts = []
                    _in_thinking_block = False
                    _in_server_block = False
                    continue
                logger.error("Anthropic streaming error: %s", e)
                raise

        self._capture_container(final)
        usage_dict = self._extract_usage_dict(final.usage)

        response = ProviderResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=final.stop_reason or "end_turn",
            usage=self._build_usage(usage_dict),
        )
        yield StreamEvent(type="done", response=response)
