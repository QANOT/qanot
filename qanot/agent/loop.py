"""Agent loop — provider iteration, tool dispatch, streaming, retries.

Mixin that holds the per-turn loop machinery for Agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator

from qanot.circuit import (
    MAX_SAME_ACTION,
    tool_call_fingerprint,
    result_fingerprint,
    strip_verbose_result,
    is_deterministic_error,
    is_loop_detected,
    is_no_progress,
)
from qanot.flush import handle_overflow
from qanot.memory import write_daily_note
from qanot.providers.base import ProviderResponse, StreamEvent, ToolCall, Usage
from qanot.providers.errors import (
    classify_error,
    PERMANENT_FAILURES,
    TRANSIENT_FAILURES,
    ERROR_AUTH,
    ERROR_BILLING,
    ERROR_RATE_LIMIT,
    ERROR_CONTEXT_OVERFLOW,
    ERROR_UNKNOWN,
)

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 25
TOOL_TIMEOUT = 30  # seconds per tool execution
# Tools that run LLM agents internally need much longer timeouts
_LONG_RUNNING_TOOLS = frozenset({
    "delegate_to_agent",
    "converse_with_agent",
    "spawn_sub_agent",
    "spawn_agent",
    "create_reel",
    "clip_video",  # transcribe (2-15 min) + LLM + cut + caption
    "publish_clip_to_meta",  # Meta Graph container polling can take minutes
})
LONG_TOOL_TIMEOUT = 1800  # 30 minutes for heavy tools (transcription of long videos)
CONVERSATION_TTL = 3600  # seconds before idle conversations are evicted
MAX_COMPACTION_RETRIES = 2  # Max overflow->compact->retry cycles
BASE_DELAY = 1.0  # seconds, base for exponential backoff
MAX_DELAY = 30.0  # seconds, backoff ceiling

# Sentinel for iteration control flow
_CONTINUE = "_CONTINUE"  # retry iteration (e.g. after overflow compaction)
_FATAL = "_FATAL"  # unrecoverable error, abort loop


class _LoopMixin:
    """Loop machinery: provider call, tool dispatch, streaming, retries."""

    async def _handle_overflow(self, messages: list[dict], user_id: str | None) -> list[dict]:
        """Handle context overflow by force-compacting the conversation."""
        return await handle_overflow(
            messages, self.provider, self.tools,
            self.config, self.context, self._conv_manager, user_id,
            self._build_system_prompt, self._build_assistant_tool_message,
        )

    def _process_tool_use(
        self,
        tool_calls: list[ToolCall],
        recent_fingerprints: list[str],
        result_history: list[tuple[str, str]],
        result_hash: str,
    ) -> str | None:
        """Check for loops and no-progress on tool calls.

        Returns an error message string if the loop should break, or None to continue.
        Updates recent_fingerprints and result_history in-place.
        """
        loop_msg = self._check_loop(tool_calls, recent_fingerprints)
        if loop_msg:
            return loop_msg

        batch_fps = [tool_call_fingerprint(tc.name, tc.input) for tc in tool_calls]
        call_key = ":".join(sorted(batch_fps))
        if is_no_progress(result_history, call_key, result_hash):
            logger.warning("No-progress loop: same call producing same result")
            self._log_error_lesson(
                f"No-progress loop: {tool_calls[0].name}",
                "Same call producing same result — need different approach",
            )
            return (
                f"Kechirasiz, {tool_calls[0].name} "
                "bir xil natija qaytarmoqda. Boshqacha yondashuv kerak."
            )
        result_history.append((call_key, result_hash))
        return None

    def _track_usage(self, response: ProviderResponse) -> None:
        """Track usage and check context threshold."""
        self.context.add_usage(
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        # Per-user cost tracking
        uid = self._current_user_id
        if uid:
            self.cost_tracker.add_usage(
                user_id=uid,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read=response.usage.cache_read_input_tokens,
                cache_write=response.usage.cache_creation_input_tokens,
                cost=response.usage.cost,
            )
        if self.context.check_threshold():
            logger.warning("Context at %.1f%% — Working Buffer activated",
                         self.context.get_context_percent())

    def _check_loop(
        self, tool_calls: list[ToolCall], recent_fingerprints: list[str]
    ) -> str | None:
        """Check for tool call loops. Returns loop message if detected, None otherwise."""
        batch_key = ":".join(sorted(tool_call_fingerprint(tc.name, tc.input) for tc in tool_calls))

        if is_loop_detected(recent_fingerprints, batch_key):
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
            user_id=self._current_user_id,
        )

    def _log_error_lesson(self, context: str, error: str) -> None:
        """Log an error to daily notes so the agent can learn from mistakes."""
        try:
            write_daily_note(
                f"**Error lesson:** {context}\n- Error: {error[:200]}",
                self.config.workspace_dir,
                user_id=str(self._current_user_id),
            )
        except Exception as e:
            logger.debug("Failed to write error lesson to daily notes: %s", e)

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> tuple[list[dict], str]:
        """Execute tool calls and return (tool_result blocks, combined result hash)."""
        tool_results: list[dict] = []
        result_parts: list[str] = []
        for tc in tool_calls:
            logger.info("Executing tool: %s", tc.name)
            timeout = LONG_TOOL_TIMEOUT if tc.name in _LONG_RUNNING_TOOLS else TOOL_TIMEOUT
            try:
                result = await self.tools.execute(
                    tc.name, tc.input, timeout=timeout,
                    workspace_dir=self.config.workspace_dir,
                )
            except Exception as e:
                logger.error("Tool %s raised unexpected exception: %s", tc.name, e)
                result = json.dumps({"error": f"Tool execution failed: {type(e).__name__}"})

            # Strip verbose detail fields from JSON results to save context
            result = strip_verbose_result(result)

            if is_deterministic_error(result):
                try:
                    result_data = json.loads(result)
                    result_data["_hint"] = "This error is permanent. Do not retry with the same parameters."
                    result = json.dumps(result_data)
                except (json.JSONDecodeError, TypeError):
                    pass

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result,
            })
            result_parts.append(result)
        combined_hash = result_fingerprint("|".join(result_parts))
        return tool_results, combined_hash

    def _handle_end_turn(
        self,
        final_text: str,
        user_message: str,
        messages: list[dict],
        usage: Usage,
    ) -> str:
        """Shared end-turn handling: append message, log, buffer, daily note.

        Returns final_text (possibly with budget warning appended).
        """
        messages.append({"role": "assistant", "content": final_text})
        # Persist per-user cost data
        self.cost_tracker.save()

        # Budget warning check
        if self._current_user_id and self.config.daily_budget_usd > 0:
            warning = self.cost_tracker.get_budget_warning(
                self._current_user_id,
                self.config.daily_budget_usd,
                self.config.budget_warning_pct,
            )
            if warning:
                final_text = f"{final_text}\n\n---\n⚠️ {warning}"

        self.session.log_assistant_message(
            text=final_text,
            usage=usage,
            parent_id=self._last_user_msg_id,
            model=self.provider.model,
            user_id=self._current_user_id,
        )

        if self.context.buffer_active:
            summary = final_text if len(final_text) <= 200 else final_text[:200] + "..."
            self.context.append_to_buffer(user_message, summary)

        write_daily_note(
            f"**User:** {user_message[:100]}...\n**Agent:** {final_text[:200]}...",
            self.config.workspace_dir,
            user_id=str(self._current_user_id),
        )
        return final_text

    async def _run_loop(
        self,
        user_message: str,
        user_id: str | None,
        messages: list[dict],
        *,
        system_prompt_override: str | None = None,
        stream: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        """Unified agent loop — yields StreamEvent objects.

        Both streaming and non-streaming paths use this. The `stream` flag
        controls whether the provider is called with chat_stream or chat.

        Yields:
            StreamEvent(type="text_delta") — incremental text (stream mode only)
            StreamEvent(type="tool_use") — tool execution signal
            StreamEvent(type="done") — final response (always the last event)
        """
        recent_fingerprints: list[str] = []
        result_history: list[tuple[str, str]] = []
        overflow_retries = 0
        cached_system: str | None = None
        cached_tool_defs: list[dict] | None = None

        for iteration in range(self._max_iterations):
            messages, system, tool_defs = await self._prepare_iteration(
                messages, user_id,
                cached_system=cached_system, cached_tool_defs=cached_tool_defs,
                user_message=user_message,
                turn_prompt_override=system_prompt_override,
            )
            if cached_system is None:
                cached_system = system
                cached_tool_defs = tool_defs

            # ── Call provider (streaming or non-streaming) ──
            response: ProviderResponse | None = None
            tool_calls: list[ToolCall] = []
            text_parts: list[str] = []

            if stream:
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
                    result = await self._handle_provider_error_stream(
                        e, messages, tool_defs, system, text_parts,
                        overflow_retries, user_id,
                    )
                    if result == _CONTINUE:
                        overflow_retries += 1
                        messages = await self._handle_overflow(messages, user_id)
                        continue
                    if isinstance(result, tuple):
                        # Fallback succeeded: (response, tool_calls, new_text_events)
                        response, tool_calls = result[0], result[1]
                        for ev in result[2]:
                            yield ev
                    else:
                        # Fatal: yield done event and return
                        yield StreamEvent(type="done", response=ProviderResponse(content=result))
                        return
            else:
                try:
                    response = await self._call_provider_with_retry(
                        messages=messages,
                        tools=tool_defs if tool_defs else None,
                        system=system,
                    )
                except Exception as e:
                    error_type = classify_error(e)
                    logger.error("Provider failed after retries: %s [%s]", e, error_type)

                    if error_type == ERROR_CONTEXT_OVERFLOW and overflow_retries < MAX_COMPACTION_RETRIES:
                        overflow_retries += 1
                        logger.info("Overflow recovery attempt %d/%d", overflow_retries, MAX_COMPACTION_RETRIES)
                        messages = await self._handle_overflow(messages, user_id)
                        continue

                    self._log_error_lesson(f"Provider error [{error_type}]", str(e))
                    error_msg = self._error_message_for_type(error_type)
                    yield StreamEvent(type="done", response=ProviderResponse(content=error_msg))
                    return

            # ── No response from stream ──
            if response is None and not tool_calls:
                break

            if response:
                self._track_usage(response)

            stop_reason = response.stop_reason if response else ("tool_use" if tool_calls else "end_turn")
            content = response.content if response else ""
            usage = response.usage if response else Usage()

            # ── Tool use ──
            if stop_reason == "tool_use" and (response.tool_calls if response else tool_calls):
                active_tool_calls = response.tool_calls if (response and response.tool_calls) else tool_calls
                messages.append(self._build_assistant_tool_message(content, active_tool_calls))
                self._log_tool_use(content, active_tool_calls, usage)

                tool_results, result_hash = await self._execute_tools(active_tool_calls)

                break_msg = self._process_tool_use(
                    active_tool_calls, recent_fingerprints, result_history, result_hash,
                )
                if break_msg:
                    messages.append({"role": "user", "content": tool_results})
                    messages.append({"role": "assistant", "content": break_msg})
                    yield StreamEvent(type="done", response=ProviderResponse(content=break_msg))
                    return

                messages.append({"role": "user", "content": tool_results})
                if stream:
                    yield StreamEvent(type="tool_use", tool_call=active_tool_calls[0] if active_tool_calls else None)

            # ── End turn ──
            elif stop_reason == "end_turn":
                final_text = content or "".join(text_parts)
                final_text = self._handle_end_turn(final_text, user_message, messages, usage)

                modified = await self.hooks.fire("on_post_turn", user_id=user_id or "", message=user_message, response=final_text)
                if modified is not None:
                    final_text = modified

                yield StreamEvent(type="done", response=response or ProviderResponse(content=final_text))
                return

            # ── Other stop reason ──
            else:
                final_text = content or "(No response)"
                messages.append({"role": "assistant", "content": final_text})
                yield StreamEvent(type="done", response=response or ProviderResponse(content=final_text))
                return

        # Max iterations exhausted
        max_iter_msg = "(Agent reached maximum iterations)"
        messages.append({"role": "assistant", "content": max_iter_msg})
        logger.warning("Agent hit max iterations (%d)", self._max_iterations)
        yield StreamEvent(type="done", response=ProviderResponse(content=max_iter_msg))

    async def _handle_provider_error_stream(
        self,
        error: Exception,
        messages: list[dict],
        tool_defs: list[dict] | None,
        system: str,
        text_parts: list[str],
        overflow_retries: int,
        user_id: str | None,
    ) -> str | tuple:
        """Handle provider errors in streaming mode.

        Returns:
            _CONTINUE — retry the iteration (after overflow compaction)
            (response, tool_calls, new_events) — fallback succeeded
            str — fatal error message
        """
        error_type = classify_error(error)
        logger.error("Stream error: %s [%s]", error, error_type)

        if error_type == ERROR_CONTEXT_OVERFLOW and overflow_retries < MAX_COMPACTION_RETRIES:
            return _CONTINUE

        # Transient errors: try non-streaming fallback
        if error_type in TRANSIENT_FAILURES:
            await asyncio.sleep(3)
            try:
                response = await self.provider.chat(
                    messages=messages,
                    tools=tool_defs if tool_defs else None,
                    system=system,
                )
                new_events: list[StreamEvent] = []
                already_streamed = "".join(text_parts)
                if response.content:
                    new_text = response.content
                    if already_streamed and new_text.startswith(already_streamed):
                        remaining = new_text[len(already_streamed):]
                        if remaining:
                            new_events.append(StreamEvent(type="text_delta", text=remaining))
                    elif not already_streamed:
                        new_events.append(StreamEvent(type="text_delta", text=new_text))
                    else:
                        new_events.append(StreamEvent(type="text_delta", text="\n" + new_text))
                return (response, response.tool_calls, new_events)
            except Exception as e2:
                logger.error("Stream fallback failed for user %s: %s", user_id, e2, exc_info=True)
                return "Xatolik yuz berdi, qaytadan urinib ko'ring."

        if error_type == ERROR_CONTEXT_OVERFLOW:
            return "Suhbat juda uzun bo'lib qoldi. /reset buyrug'ini yuboring va qaytadan boshlang."

        return "Xatolik yuz berdi, qaytadan urinib ko'ring."

    @staticmethod
    def _error_message_for_type(error_type: str) -> str:
        """Map error type to user-facing Uzbek message."""
        messages = {
            ERROR_RATE_LIMIT: "Limitga yetdik, biroz kutib qaytadan urinib ko'ring.",
            ERROR_AUTH: "API kalitda xatolik. Administrator bilan bog'laning.",
            ERROR_BILLING: "API hisob muammosi. Administrator bilan bog'laning.",
            ERROR_CONTEXT_OVERFLOW: "Suhbat juda uzun bo'lib qoldi. /reset buyrug'ini yuboring va qaytadan boshlang.",
        }
        return messages.get(error_type, "Xatolik yuz berdi, qaytadan urinib ko'ring.")

    async def _call_provider_with_retry(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        system: str,
        max_retries: int = 3,
    ) -> ProviderResponse:
        """Call the LLM provider with exponential backoff and jitter."""
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

                # Retry transient errors, and unknown errors on first attempt
                retryable = (
                    error_type in TRANSIENT_FAILURES
                    or (attempt == 0 and error_type == ERROR_UNKNOWN)
                )
                if attempt < max_retries and retryable:
                    base = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                    jitter = random.uniform(0, 0.25 * base)
                    backoff = base + jitter
                    logger.info("Retrying in %.1fs...", backoff)
                    await asyncio.sleep(backoff)
                    continue

                raise

        # Should not reach here, but defend against it
        if last_error is not None:
            raise last_error
        raise RuntimeError("Provider call failed with no captured error")
