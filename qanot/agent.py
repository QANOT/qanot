"""Core agent loop — the heart of Qanot AI."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Callable, Awaitable

from qanot.config import Config
from qanot.context import ContextTracker, truncate_tool_result
from qanot.memory import wal_scan, wal_write, write_daily_note
from qanot.prompt import build_system_prompt
from qanot.providers.base import LLMProvider, ProviderResponse, StreamEvent, ToolCall, Usage
from qanot.providers.errors import (
    classify_error,
    PERMANENT_FAILURES,
    TRANSIENT_FAILURES,
    ERROR_AUTH,
    ERROR_BILLING,
    ERROR_RATE_LIMIT,
)
from qanot.session import SessionWriter

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 25
MAX_SAME_ACTION = 3  # Break after N identical consecutive tool calls
TOOL_TIMEOUT = 30  # seconds per tool execution
CONVERSATION_TTL = 3600  # seconds before idle conversations are evicted

# Errors that should NOT be retried (deterministic failures)
DETERMINISTIC_ERRORS = (
    "unknown tool",
    "missing required",
    "invalid parameter",
    "not found",
    "permission denied",
    "validation error",
    "invalid input",
)

def _tool_call_fingerprint(name: str, input_data: dict) -> str:
    """Hash a tool call for duplicate detection."""
    raw = f"{name}:{json.dumps(input_data, sort_keys=True)}"
    return hashlib.md5(raw.encode()).hexdigest()


def _is_deterministic_error(result: str) -> bool:
    """Check if a tool error is deterministic (should not be retried)."""
    try:
        data = json.loads(result)
        error = data.get("error", "").lower()
        return any(marker in error for marker in DETERMINISTIC_ERRORS)
    except (json.JSONDecodeError, AttributeError):
        return False


def _is_loop_detected(recent_fingerprints: list[str], new_key: str) -> bool:
    """Check if adding new_key would create a loop BEFORE executing tools.

    Detects:
    1. Same exact call repeated N times
    2. Alternating patterns (A-B-A-B)
    """
    # Check exact repetition
    recent_same = sum(1 for fp in recent_fingerprints if fp == new_key)
    if recent_same >= MAX_SAME_ACTION - 1:  # Would be Nth occurrence
        return True

    # Check alternating pattern (A-B-A-B) in last 4
    if len(recent_fingerprints) >= 3:
        last4 = recent_fingerprints[-3:] + [new_key]
        if len(last4) == 4 and last4[0] == last4[2] and last4[1] == last4[3] and last4[0] != last4[1]:
            return True

    return False


def _repair_messages(messages: list[dict]) -> list[dict]:
    """Repair message history to fix common corruption issues.

    Fixes:
    - Orphaned tool_result blocks (no matching tool_use)
    - Consecutive same-role messages (merge or remove)
    """
    if not messages:
        return messages

    repaired = []
    # Track tool_use IDs that exist
    active_tool_ids: set[str] = set()

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "assistant" and isinstance(content, list):
            # Track tool_use IDs from assistant messages
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    active_tool_ids.add(block.get("id", ""))
            repaired.append(msg)

        elif role == "user" and isinstance(content, list):
            # Filter tool_results: only keep those with matching tool_use
            valid_results = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    if tool_use_id in active_tool_ids:
                        valid_results.append(block)
                        active_tool_ids.discard(tool_use_id)
                    else:
                        logger.warning("Removing orphaned tool_result: %s", tool_use_id)
                else:
                    valid_results.append(block)

            if valid_results:
                repaired.append({"role": "user", "content": valid_results})
        else:
            repaired.append(msg)

    return repaired


class ToolRegistry:
    """Registry of available tools."""

    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Callable[[dict], Awaitable[str]]] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable[[dict], Awaitable[str]],
    ) -> None:
        """Register a tool with its handler."""
        self._tools[name] = {
            "name": name,
            "description": description,
            "input_schema": parameters,
        }
        self._handlers[name] = handler

    def get_definitions(self) -> list[dict]:
        """Get tool definitions for the LLM."""
        return list(self._tools.values())

    async def execute(self, name: str, input_data: dict, timeout: float = TOOL_TIMEOUT) -> str:
        """Execute a tool by name with timeout protection."""
        handler = self._handlers.get(name)
        if not handler:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            result = await asyncio.wait_for(handler(input_data), timeout=timeout)
            # Truncate oversized results
            return truncate_tool_result(result)
        except asyncio.TimeoutError:
            logger.error("Tool %s timed out after %ds", name, timeout)
            return json.dumps({"error": f"Tool timed out after {timeout}s"})
        except Exception as e:
            logger.error("Tool %s failed: %s", name, e)
            return json.dumps({"error": str(e)})

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())


class Agent:
    """Core agent that runs the tool_use loop."""

    def __init__(
        self,
        config: Config,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        session: SessionWriter | None = None,
        context: ContextTracker | None = None,
        prompt_mode: str = "full",
    ):
        self.config = config
        self.provider = provider
        self.tools = tool_registry
        self.session = session or SessionWriter(config.sessions_dir)
        self.context = context or ContextTracker(
            max_tokens=config.max_context_tokens,
            workspace_dir=config.workspace_dir,
        )
        self.prompt_mode = prompt_mode
        # Per-user conversation histories keyed by user_id.
        # None key is used for non-user contexts (cron jobs, etc.)
        self._conversations: dict[str | None, list[dict]] = {}
        self._locks: dict[str | None, asyncio.Lock] = {}
        self._last_active: dict[str | None, float] = {}
        self._last_user_msg_id = ""

    def _get_lock(self, user_id: str | None) -> asyncio.Lock:
        """Get or create a per-user lock for write safety."""
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    def _evict_stale(self) -> None:
        """Remove conversation state for users idle longer than CONVERSATION_TTL."""
        now = time.monotonic()
        stale = [
            uid for uid, ts in self._last_active.items()
            if now - ts > CONVERSATION_TTL
        ]
        for uid in stale:
            self._conversations.pop(uid, None)
            self._locks.pop(uid, None)
            self._last_active.pop(uid, None)
            logger.debug("Evicted stale conversation for user_id=%s", uid)

    def _get_messages(self, user_id: str | None = None) -> list[dict]:
        """Get or create conversation history for a user."""
        self._evict_stale()
        self._last_active[user_id] = time.monotonic()
        if user_id not in self._conversations:
            self._conversations[user_id] = []
        return self._conversations[user_id]

    def _build_system_prompt(self) -> str:
        """Build the system prompt from workspace files."""
        return build_system_prompt(
            workspace_dir=self.config.workspace_dir,
            owner_name=self.config.owner_name,
            bot_name=self.config.bot_name,
            timezone_str=self.config.timezone,
            context_percent=self.context.get_context_percent(),
            total_tokens=self.context.total_tokens,
            mode=self.prompt_mode,
        )

    def _prepare_turn(self, user_message: str, messages: list[dict]) -> str:
        """Shared turn setup: WAL scan, compaction recovery, add user message.

        Returns the (possibly modified) user_message.
        """
        # WAL Protocol: scan user message BEFORE generating response
        wal_entries = wal_scan(user_message)
        if wal_entries:
            wal_write(wal_entries, self.config.workspace_dir)
            logger.debug("WAL: wrote %d entries before responding", len(wal_entries))

        # Check for compaction recovery
        if self.context.detect_compaction(messages):
            recovery = self.context.recover_from_compaction()
            if recovery:
                user_message = f"{user_message}\n\n---\n\n[COMPACTION RECOVERY]\n{recovery}"
                logger.info("Compaction recovery injected")

        # Add user message to conversation
        messages.append({"role": "user", "content": user_message})

        # Log to session
        self._last_user_msg_id = self.session.log_user_message(user_message)
        return user_message

    def _prepare_iteration(self, messages: list[dict], user_id: str | None) -> tuple[list[dict], str, list[dict]]:
        """Shared per-iteration prep: compaction, repair, build prompt/tools.

        Returns (messages, system_prompt, tool_defs).
        """
        if self.context.needs_compaction() and len(messages) > 6:
            compacted = self.context.compact_messages(messages)
            self._conversations[user_id] = compacted
            messages = compacted
            logger.info("Proactive compaction triggered at %.1f%%",
                       self.context.get_context_percent())

        messages = _repair_messages(messages)
        self._conversations[user_id] = messages

        system = self._build_system_prompt()
        tool_defs = self.tools.get_definitions()
        return messages, system, tool_defs

    def _track_usage(self, response: ProviderResponse) -> None:
        """Track usage and check context threshold."""
        self.context.add_usage(
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        if self.context.check_threshold():
            logger.warning("Context at %.1f%% — Working Buffer activated",
                         self.context.get_context_percent())

    def _check_loop(
        self, tool_calls: list[ToolCall], recent_fingerprints: list[str]
    ) -> str | None:
        """Check for tool call loops. Returns loop message if detected, None otherwise."""
        batch_fps = [_tool_call_fingerprint(tc.name, tc.input) for tc in tool_calls]
        batch_key = ":".join(sorted(batch_fps))

        if _is_loop_detected(recent_fingerprints, batch_key):
            logger.warning(
                "Loop detected BEFORE execution: %s (count=%d)",
                tool_calls[0].name, MAX_SAME_ACTION,
            )
            return (
                f"Kechirasiz, {tool_calls[0].name} "
                f"amali takrorlanmoqda. Iltimos, boshqacha so'rov bering."
            )

        recent_fingerprints.append(batch_key)
        return None

    def _build_assistant_tool_message(
        self, text: str | None, tool_calls: list[ToolCall]
    ) -> dict:
        """Build an assistant message with text + tool_use blocks."""
        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        for tc in tool_calls:
            content.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.input,
            })
        return {"role": "assistant", "content": content}

    def _log_tool_use(
        self, text: str, tool_calls: list[ToolCall], usage: Usage
    ) -> None:
        """Log tool uses to session."""
        self.session.log_assistant_message(
            text=text,
            tool_uses=[{"name": tc.name, "input": tc.input} for tc in tool_calls],
            usage=usage,
            parent_id=self._last_user_msg_id,
            model=self.provider.model,
        )

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> list[dict]:
        """Execute tool calls and return tool_result blocks."""
        tool_results: list[dict] = []
        for tc in tool_calls:
            logger.info("Executing tool: %s", tc.name)
            result = await self.tools.execute(tc.name, tc.input)

            if _is_deterministic_error(result):
                result_data = json.loads(result)
                result_data["_hint"] = "This error is permanent. Do not retry with the same parameters."
                result = json.dumps(result_data)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result,
            })
        return tool_results

    def _handle_end_turn(
        self,
        final_text: str,
        user_message: str,
        messages: list[dict],
        usage: Usage,
    ) -> None:
        """Shared end-turn handling: append message, log, buffer, daily note."""
        messages.append({"role": "assistant", "content": final_text})

        self.session.log_assistant_message(
            text=final_text,
            usage=usage,
            parent_id=self._last_user_msg_id,
            model=self.provider.model,
        )

        if self.context.buffer_active:
            summary = final_text[:200] + "..." if len(final_text) > 200 else final_text
            self.context.append_to_buffer(user_message, summary)

        write_daily_note(
            f"**User:** {user_message[:100]}...\n**Agent:** {final_text[:200]}...",
            self.config.workspace_dir,
        )

    async def _call_provider_with_retry(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        system: str,
        max_retries: int = 2,
    ) -> ProviderResponse:
        """Call the LLM provider with retry logic for transient errors."""
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return await self.provider.chat(
                    messages=messages,
                    tools=tools,
                    system=system,
                )
            except Exception as e:
                last_error = e
                error_type = classify_error(e)
                logger.warning(
                    "Provider error (attempt %d/%d): %s [%s]",
                    attempt + 1, max_retries + 1, e, error_type,
                )

                # Don't retry permanent errors
                if error_type in PERMANENT_FAILURES:
                    raise

                # Retry transient errors with backoff
                if attempt < max_retries and error_type in TRANSIENT_FAILURES:
                    backoff = min(2 ** attempt * 2, 30)  # 2s, 4s, max 30s
                    logger.info("Retrying in %ds...", backoff)
                    await asyncio.sleep(backoff)
                    continue

                raise

        raise last_error  # Should not reach here

    async def run_turn(self, user_message: str, user_id: str | None = None) -> str:
        """Process a user message through the agent loop.

        Args:
            user_message: The user's text input.
            user_id: Unique user identifier for conversation isolation.

        Returns the final text response.
        """
        async with self._get_lock(user_id):
            return await self._run_turn_impl(user_message, user_id)

    async def _run_turn_impl(self, user_message: str, user_id: str | None) -> str:
        """Internal implementation of run_turn (called under lock)."""
        messages = self._get_messages(user_id)
        user_message = self._prepare_turn(user_message, messages)

        final_text = ""
        recent_fingerprints: list[str] = []

        for iteration in range(MAX_ITERATIONS):
            messages, system, tool_defs = self._prepare_iteration(messages, user_id)

            try:
                response = await self._call_provider_with_retry(
                    messages=messages,
                    tools=tool_defs if tool_defs else None,
                    system=system,
                )
            except Exception as e:
                error_type = classify_error(e)
                logger.error("Provider failed after retries: %s [%s]", e, error_type)
                if error_type == ERROR_RATE_LIMIT:
                    return "Limitga yetdik, biroz kutib qaytadan urinib ko'ring."
                elif error_type == ERROR_AUTH:
                    return "API kalitda xatolik. Administrator bilan bog'laning."
                elif error_type == ERROR_BILLING:
                    return "API hisob muammosi. Administrator bilan bog'laning."
                return "Xatolik yuz berdi, qaytadan urinib ko'ring."

            self._track_usage(response)

            if response.stop_reason == "tool_use" and response.tool_calls:
                loop_msg = self._check_loop(response.tool_calls, recent_fingerprints)
                if loop_msg:
                    final_text = loop_msg
                    messages.append({"role": "assistant", "content": final_text})
                    break

                messages.append(
                    self._build_assistant_tool_message(response.content, response.tool_calls)
                )
                self._log_tool_use(response.content, response.tool_calls, response.usage)

                tool_results = await self._execute_tools(response.tool_calls)
                messages.append({"role": "user", "content": tool_results})

            elif response.stop_reason == "end_turn":
                final_text = response.content
                self._handle_end_turn(final_text, user_message, messages, response.usage)
                break
            else:
                final_text = response.content or "(No response)"
                messages.append({"role": "assistant", "content": final_text})
                break
        else:
            final_text = "(Agent reached maximum iterations)"
            logger.warning("Agent hit max iterations (%d)", MAX_ITERATIONS)

        return final_text

    async def run_turn_stream(
        self, user_message: str, user_id: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        """Process a user message with streaming.

        Yields StreamEvent objects as they arrive from the provider.
        The final event has type="done" with the complete ProviderResponse.
        Tool-use iterations are handled internally; text deltas from each
        iteration are yielded as they arrive.
        """
        async with self._get_lock(user_id):
            async for event in self._run_turn_stream_impl(user_message, user_id):
                yield event

    async def _run_turn_stream_impl(
        self, user_message: str, user_id: str | None
    ) -> AsyncIterator[StreamEvent]:
        """Internal streaming implementation (called under lock)."""
        messages = self._get_messages(user_id)
        user_message = self._prepare_turn(user_message, messages)

        recent_fingerprints: list[str] = []

        for iteration in range(MAX_ITERATIONS):
            messages, system, tool_defs = self._prepare_iteration(messages, user_id)

            response: ProviderResponse | None = None
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []

            try:
                async for event in self.provider.chat_stream(
                    messages=messages,
                    tools=tool_defs if tool_defs else None,
                    system=system,
                ):
                    if event.type == "text_delta":
                        text_parts.append(event.text)
                        yield event
                    elif event.type == "tool_use" and event.tool_call:
                        tool_calls.append(event.tool_call)
                    elif event.type == "done":
                        response = event.response
            except Exception as e:
                error_type = classify_error(e)
                logger.error("Stream error: %s [%s]", e, error_type)

                # Try non-streaming fallback for transient errors
                if error_type in TRANSIENT_FAILURES:
                    await asyncio.sleep(3)
                    try:
                        response = await self.provider.chat(
                            messages=messages,
                            tools=tool_defs if tool_defs else None,
                            system=system,
                        )
                        if response.content:
                            yield StreamEvent(type="text_delta", text=response.content)
                            text_parts.append(response.content)
                        tool_calls = response.tool_calls
                    except Exception:
                        yield StreamEvent(
                            type="done",
                            response=ProviderResponse(content="Xatolik yuz berdi, qaytadan urinib ko'ring."),
                        )
                        return
                else:
                    yield StreamEvent(
                        type="done",
                        response=ProviderResponse(content="Xatolik yuz berdi, qaytadan urinib ko'ring."),
                    )
                    return

            if response is None and not tool_calls:
                break

            if response:
                self._track_usage(response)

            stop_reason = response.stop_reason if response else ("tool_use" if tool_calls else "end_turn")

            if stop_reason == "tool_use" and tool_calls:
                loop_msg = self._check_loop(tool_calls, recent_fingerprints)
                if loop_msg:
                    yield StreamEvent(
                        type="done",
                        response=ProviderResponse(content=loop_msg),
                    )
                    return

                text = response.content if response else ""
                usage = response.usage if response else Usage()

                messages.append(self._build_assistant_tool_message(text, tool_calls))
                self._log_tool_use(text, tool_calls, usage)

                tool_results = await self._execute_tools(tool_calls)
                messages.append({"role": "user", "content": tool_results})

                yield StreamEvent(type="tool_use")

            elif stop_reason == "end_turn":
                final_text = response.content if response else "".join(text_parts)
                usage = response.usage if response else Usage()
                self._handle_end_turn(final_text, user_message, messages, usage)

                yield StreamEvent(type="done", response=response or ProviderResponse(content=final_text))
                return
            else:
                final_text = (response.content if response else "") or "(No response)"
                messages.append({"role": "assistant", "content": final_text})
                yield StreamEvent(type="done", response=response or ProviderResponse(content=final_text))
                return

        yield StreamEvent(
            type="done",
            response=ProviderResponse(content="(Agent reached maximum iterations)"),
        )

    def reset(self, user_id: str | None = None) -> None:
        """Reset conversation state for a user, or all if user_id is None."""
        if user_id is not None:
            self._conversations.pop(user_id, None)
            self._locks.pop(user_id, None)
            self._last_active.pop(user_id, None)
        else:
            self._conversations.clear()
            self._locks.clear()
            self._last_active.clear()


async def spawn_isolated_agent(
    config: Config,
    provider: LLMProvider,
    tool_registry: ToolRegistry,
    prompt: str,
    session_id: str | None = None,
) -> str:
    """Spawn an isolated agent that runs independently.

    Used for cron jobs and background tasks.
    Returns the agent's final response.
    """
    session = SessionWriter(config.sessions_dir)
    if session_id:
        session.new_session(session_id)

    context = ContextTracker(
        max_tokens=config.max_context_tokens,
        workspace_dir=config.workspace_dir,
    )

    agent = Agent(
        config=config,
        provider=provider,
        tool_registry=tool_registry,
        session=session,
        context=context,
        prompt_mode="minimal",
    )

    result = await agent.run_turn(prompt)
    return result
