"""Core agent loop — the heart of Qanot AI."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
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
        self._last_user_msg_id = ""

    def _get_lock(self, user_id: str | None) -> asyncio.Lock:
        """Get or create a per-user lock for write safety."""
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    def _get_messages(self, user_id: str | None = None) -> list[dict]:
        """Get or create conversation history for a user."""
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
        messages.append({
            "role": "user",
            "content": user_message,
        })

        # Log to session
        self._last_user_msg_id = self.session.log_user_message(user_message)

        # Agent loop with circuit breaker
        final_text = ""
        recent_fingerprints: list[str] = []

        for iteration in range(MAX_ITERATIONS):
            # ── Proactive compaction ──
            if self.context.needs_compaction() and len(messages) > 6:
                messages_ref = self._get_messages(user_id)
                compacted = self.context.compact_messages(messages_ref)
                self._conversations[user_id] = compacted
                messages = compacted
                logger.info("Proactive compaction triggered at %.1f%%",
                           self.context.get_context_percent())

            # Repair messages before sending
            messages = _repair_messages(messages)
            self._conversations[user_id] = messages

            system = self._build_system_prompt()
            tool_defs = self.tools.get_definitions()

            # Call LLM with retry
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
                return f"Xatolik yuz berdi, qaytadan urinib ko'ring."

            # Track usage
            self.context.add_usage(
                response.usage.input_tokens,
                response.usage.output_tokens,
            )

            # Check 60% threshold
            if self.context.check_threshold():
                logger.warning("Context at %.1f%% — Working Buffer activated",
                             self.context.get_context_percent())

            if response.stop_reason == "tool_use" and response.tool_calls:
                # ── Pre-execution loop detection ──
                batch_fps = [
                    _tool_call_fingerprint(tc.name, tc.input)
                    for tc in response.tool_calls
                ]
                batch_key = ":".join(sorted(batch_fps))

                if _is_loop_detected(recent_fingerprints, batch_key):
                    logger.warning(
                        "Loop detected BEFORE execution: %s (count=%d)",
                        response.tool_calls[0].name, MAX_SAME_ACTION,
                    )
                    final_text = (
                        f"Kechirasiz, {response.tool_calls[0].name} "
                        f"amali takrorlanmoqda. Iltimos, boshqacha so'rov bering."
                    )
                    messages.append({"role": "assistant", "content": final_text})
                    break

                recent_fingerprints.append(batch_key)

                # Build assistant message with text + tool_use blocks
                assistant_content: list[dict] = []
                if response.content:
                    assistant_content.append({"type": "text", "text": response.content})
                for tc in response.tool_calls:
                    assistant_content.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                    })

                messages.append({
                    "role": "assistant",
                    "content": assistant_content,
                })

                # Log tool uses
                tool_use_dicts = [{"name": tc.name, "input": tc.input} for tc in response.tool_calls]
                self.session.log_assistant_message(
                    text=response.content,
                    tool_uses=tool_use_dicts,
                    usage=response.usage,
                    parent_id=self._last_user_msg_id,
                    model=self.provider.model,
                )

                # Execute tools and collect results (truncated)
                tool_results: list[dict] = []
                for tc in response.tool_calls:
                    logger.info("Executing tool: %s", tc.name)
                    result = await self.tools.execute(tc.name, tc.input)

                    # ── Error classification ──
                    if _is_deterministic_error(result):
                        # Inject hint so LLM doesn't retry the same call
                        result_data = json.loads(result)
                        result_data["_hint"] = "This error is permanent. Do not retry with the same parameters."
                        result = json.dumps(result_data)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result,
                    })

                # Add tool results as user message
                messages.append({
                    "role": "user",
                    "content": tool_results,
                })

            elif response.stop_reason == "end_turn":
                final_text = response.content

                # Add assistant response to messages
                messages.append({
                    "role": "assistant",
                    "content": response.content,
                })

                # Log final response
                self.session.log_assistant_message(
                    text=response.content,
                    usage=response.usage,
                    parent_id=self._last_user_msg_id,
                    model=self.provider.model,
                )

                # Write to working buffer if active
                if self.context.buffer_active:
                    summary = final_text[:200] + "..." if len(final_text) > 200 else final_text
                    self.context.append_to_buffer(user_message, summary)

                # Write daily note
                write_daily_note(
                    f"**User:** {user_message[:100]}...\n**Agent:** {final_text[:200]}...",
                    self.config.workspace_dir,
                )

                break
            else:
                # Unknown stop reason
                final_text = response.content or "(No response)"
                messages.append({
                    "role": "assistant",
                    "content": final_text,
                })
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

        # WAL Protocol
        wal_entries = wal_scan(user_message)
        if wal_entries:
            wal_write(wal_entries, self.config.workspace_dir)

        # Compaction recovery
        if self.context.detect_compaction(messages):
            recovery = self.context.recover_from_compaction()
            if recovery:
                user_message = f"{user_message}\n\n---\n\n[COMPACTION RECOVERY]\n{recovery}"

        messages.append({"role": "user", "content": user_message})
        self._last_user_msg_id = self.session.log_user_message(user_message)

        recent_fingerprints: list[str] = []

        for iteration in range(MAX_ITERATIONS):
            # ── Proactive compaction ──
            if self.context.needs_compaction() and len(messages) > 6:
                messages_ref = self._get_messages(user_id)
                compacted = self.context.compact_messages(messages_ref)
                self._conversations[user_id] = compacted
                messages = compacted

            # Repair messages
            messages = _repair_messages(messages)
            self._conversations[user_id] = messages

            system = self._build_system_prompt()
            tool_defs = self.tools.get_definitions()

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

                # Try non-streaming retry for transient errors
                if error_type in TRANSIENT_FAILURES:
                    backoff = 3
                    logger.info("Stream failed, retrying non-stream in %ds...", backoff)
                    await asyncio.sleep(backoff)
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
                    except Exception as e2:
                        error_msg = "Xatolik yuz berdi, qaytadan urinib ko'ring."
                        yield StreamEvent(
                            type="done",
                            response=ProviderResponse(content=error_msg),
                        )
                        return
                else:
                    error_msg = "Xatolik yuz berdi, qaytadan urinib ko'ring."
                    yield StreamEvent(
                        type="done",
                        response=ProviderResponse(content=error_msg),
                    )
                    return

            if response is None and not tool_calls:
                break

            # Track usage
            if response:
                self.context.add_usage(
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                )
                if self.context.check_threshold():
                    logger.warning(
                        "Context at %.1f%% — Working Buffer activated",
                        self.context.get_context_percent(),
                    )

            stop_reason = response.stop_reason if response else ("tool_use" if tool_calls else "end_turn")

            if stop_reason == "tool_use" and tool_calls:
                # ── Pre-execution loop detection ──
                batch_fps = [
                    _tool_call_fingerprint(tc.name, tc.input)
                    for tc in tool_calls
                ]
                batch_key = ":".join(sorted(batch_fps))

                if _is_loop_detected(recent_fingerprints, batch_key):
                    logger.warning("Loop detected in stream BEFORE execution")
                    yield StreamEvent(
                        type="done",
                        response=ProviderResponse(
                            content=f"Kechirasiz, {tool_calls[0].name} amali takrorlanmoqda.",
                        ),
                    )
                    return

                recent_fingerprints.append(batch_key)

                # Build assistant message
                assistant_content: list[dict] = []
                if response and response.content:
                    assistant_content.append({"type": "text", "text": response.content})
                for tc in tool_calls:
                    assistant_content.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                    })
                messages.append({"role": "assistant", "content": assistant_content})

                self.session.log_assistant_message(
                    text=response.content if response else "",
                    tool_uses=[{"name": tc.name, "input": tc.input} for tc in tool_calls],
                    usage=response.usage if response else Usage(),
                    parent_id=self._last_user_msg_id,
                    model=self.provider.model,
                )

                # Execute tools with timeout and truncation
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
                messages.append({"role": "user", "content": tool_results})

                # Signal that tools ran and we're continuing
                yield StreamEvent(type="tool_use")

            elif stop_reason == "end_turn":
                final_text = response.content if response else "".join(text_parts)
                messages.append({"role": "assistant", "content": final_text})

                self.session.log_assistant_message(
                    text=final_text,
                    usage=response.usage if response else Usage(),
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

                yield StreamEvent(type="done", response=response or ProviderResponse(content=final_text))
                return
            else:
                final_text = (response.content if response else "") or "(No response)"
                messages.append({"role": "assistant", "content": final_text})
                yield StreamEvent(type="done", response=response or ProviderResponse(content=final_text))
                return

        # Max iterations reached
        yield StreamEvent(
            type="done",
            text="(Agent reached maximum iterations)",
            response=ProviderResponse(content="(Agent reached maximum iterations)"),
        )

    def reset(self, user_id: str | None = None) -> None:
        """Reset conversation state for a user, or all if user_id is None."""
        if user_id is not None:
            self._conversations.pop(user_id, None)
        else:
            self._conversations.clear()


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
