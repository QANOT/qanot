"""Core agent loop — the heart of Qanot AI."""

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
from qanot.config import Config
from qanot.context import ContextTracker, CostTracker
from qanot.conversation import ConversationManager
from qanot.flush import memory_flush, summarize_for_compaction, handle_overflow
from qanot.memory import wal_scan, wal_write, write_daily_note
from qanot.messages import strip_thinking_blocks, repair_messages
from qanot.prompt import build_system_prompt
from qanot.providers.base import LLMProvider, ProviderResponse, StreamEvent, ToolCall, Usage
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
from qanot.hooks import HookRegistry
from qanot.registry import ToolRegistry  # re-export for compat
from qanot.session import SessionWriter

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
        system_prompt_override: str = "",
        hooks=None,
        _is_child: bool = False,
        max_iterations: int = MAX_ITERATIONS,
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
        self._system_prompt_override = system_prompt_override
        self._current_user_id: str = ""
        self._current_chat_id: int | None = None
        self._current_message_id: int | None = None
        self._rag_indexer = None  # Set by main.py when RAG is enabled
        self.cost_tracker = CostTracker(config.workspace_dir)
        self._max_iterations = max_iterations
        self._is_child = _is_child
        # Per-user conversation histories keyed by user_id.
        # None key is used for non-user contexts (cron jobs, etc.)
        self._conv_manager = ConversationManager(
            history_limit=config.history_limit,
            ttl=CONVERSATION_TTL,
        )
        # Backward compat: expose raw dict for dashboard / tests
        self._conversations = self._conv_manager._conversations
        self._last_user_msg_id = ""
        # Loaded skills (populated by load_skills)
        self._skills: list = []
        # Per-user pending images queue (populated by generate_image tool)
        self._pending_images: dict[str, list[str]] = {}
        # Per-user pending files queue (populated by send_file tool)
        self._pending_files: dict[str, list[str]] = {}
        # Lifecycle hooks
        self.hooks: HookRegistry = hooks or HookRegistry()
        # Only main agent sets _instance (child agents must not clobber it)
        if not _is_child:
            Agent._instance = self

    # Class-level reference for tools to push images without direct agent access
    _instance: "Agent | None" = None

    @classmethod
    def _push_pending_image(cls, user_id: str, image_path: str) -> None:
        """Push an image path to the pending queue for a user."""
        if cls._instance is not None:
            cls._instance._pending_images.setdefault(user_id, []).append(image_path)

    def pop_pending_images(self, user_id: str) -> list[str]:
        """Pop all pending image paths for a user."""
        return self._pending_images.pop(user_id, [])

    def pop_pending_files(self, user_id: str) -> list[str]:
        """Pop all pending file paths for a user."""
        return self._pending_files.pop(user_id, [])

    def attach_rag(self, rag_indexer) -> None:
        """Attach RAG indexer for auto-context injection."""
        self._rag_indexer = rag_indexer

    def load_skills(self, workspace_dir: str) -> None:
        """Discover and load skills from workspace/skills/ directory."""
        from qanot.skills import discover_skills
        self._skills = discover_skills(workspace_dir)

    @property
    def current_user_id(self) -> str:
        """Current user ID being processed (for RAG user-scoped queries)."""
        return self._current_user_id

    @property
    def current_chat_id(self) -> int | None:
        """Current Telegram chat ID being processed (for sub-agent delivery)."""
        return self._current_chat_id

    @property
    def current_message_id(self) -> int | None:
        """Current Telegram message_id being processed (for message scrubbing tools)."""
        return self._current_message_id

    def get_conversation(self, user_id: str | None) -> list[dict]:
        """Get conversation history for a user (read-only view)."""
        return self._conv_manager.get_messages(user_id)

    def _get_lock(self, user_id: str | None) -> asyncio.Lock:
        """Get or create a per-user lock for write safety."""
        return self._conv_manager.get_lock(user_id)

    def _remove_user_state(self, user_id: str | None) -> None:
        """Remove all per-user state (conversation, lock, activity timestamp)."""
        self._conv_manager.remove(user_id)

    def _evict_stale(self) -> None:
        """Remove conversation state for users idle longer than CONVERSATION_TTL."""
        self._conv_manager.evict_stale()

    def _get_messages(self, user_id: str | None = None) -> list[dict]:
        """Get or create conversation history for a user.

        On first access for a user (after restart or TTL eviction),
        restores recent history from JSONL session files so the bot
        remembers previous conversations.
        """
        self._evict_stale()
        if not self._conv_manager.has_user(user_id):
            # Try to restore from session history
            restored: list[dict] = []
            if user_id is not None:
                try:
                    restored = self.session.restore_history(
                        user_id=str(user_id),
                        max_turns=self.config.history_limit,
                    )
                except Exception as e:
                    logger.warning("Session restore failed for user %s: %s", user_id, e)
            return self._conv_manager.restore_from_session(user_id, restored)
        return self._conv_manager.ensure_messages(user_id)

    def _build_system_prompt(self, active_skills_content: str = "", *, turn_prompt_override: str | None = None) -> str:
        """Build the system prompt from workspace files."""
        if turn_prompt_override:
            return turn_prompt_override
        if self._system_prompt_override:
            return self._system_prompt_override

        from qanot.skills import build_skill_index
        skill_index = build_skill_index(self._skills) if self._skills else ""

        return build_system_prompt(
            workspace_dir=self.config.workspace_dir,
            owner_name=self.config.owner_name,
            bot_name=self.config.bot_name,
            timezone_str=self.config.timezone,
            context_percent=self.context.get_context_percent(),
            total_tokens=self.context.total_tokens,
            mode=self.prompt_mode,
            user_id=str(self._current_user_id) if self._current_user_id else "",
            skill_index=skill_index,
            active_skills_content=active_skills_content,
        )

    async def _prepare_turn(self, user_message: str, messages: list[dict], *, images: list[dict] | None = None) -> str:
        """Shared turn setup: WAL scan, RAG context, compaction recovery, add user message.

        Returns the (possibly modified) user_message.
        """
        # Unicode sanitization: strip invisible/dangerous chars before processing
        from qanot.utils import sanitize_unicode
        user_message = sanitize_unicode(user_message)

        # WAL Protocol: scan user message BEFORE generating response.
        # Skip for isolated/cron agents (no user_id) — their prompts are internal
        # instructions, not user utterances, and would produce junk SESSION-STATE.md
        # entries (e.g. the daily briefing prompt matching "decision" patterns).
        if self._current_user_id:
            wal_entries = wal_scan(user_message)
            if wal_entries:
                wal_write(wal_entries, self.config.workspace_dir, user_id=str(self._current_user_id))
                logger.debug("WAL: wrote %d entries before responding", len(wal_entries))

        # RAG context injection: auto-inject relevant memory for dumb models
        # "auto"/"always" = inject, "agentic" = skip (model uses rag_search tool)
        if (
            self._rag_indexer is not None
            and self.config.rag_mode in ("auto", "always")
            and len(user_message.strip()) > 10  # skip trivial messages like "hi"
        ):
            try:
                hints = await self._rag_indexer.search(
                    user_message, top_k=3, user_id=self._current_user_id or None,
                )
                if hints:
                    hint_text = "\n".join(
                        f"- [{h['file']}] {h['content'][:200]}" for h in hints[:3]
                    )
                    cap = self.config.max_memory_injection_chars
                    if len(hint_text) > cap:
                        hint_text = hint_text[:cap] + "\n[... truncated]"
                    user_message = (
                        f"{user_message}\n\n---\n"
                        f"[MEMORY CONTEXT — relevant past information]\n{hint_text}"
                    )
                    logger.debug("RAG: injected %d memory hints", len(hints))
            except Exception as e:
                logger.warning("RAG context injection failed: %s", e)

        # Link understanding: auto-fetch URLs in user messages (non-blocking, 3s max)
        if len(user_message.strip()) > 10:
            try:
                from qanot.links import fetch_link_previews

                link_context = await asyncio.wait_for(
                    fetch_link_previews(user_message), timeout=3.0,
                )
                if link_context:
                    cap = self.config.max_memory_injection_chars
                    if len(link_context) > cap:
                        link_context = link_context[:cap] + "\n[... truncated]"
                    user_message = f"{user_message}\n\n---\n{link_context}"
            except asyncio.TimeoutError:
                logger.debug("Link preview skipped (>3s timeout)")
            except Exception as e:
                logger.debug("Link preview injection failed: %s", e)

        # Check for compaction recovery
        if self.context.detect_compaction(messages):
            recovery = self.context.recover_from_compaction()
            if recovery:
                cap = self.config.max_memory_injection_chars
                if len(recovery) > cap:
                    recovery = recovery[:cap] + "\n[... truncated]"
                user_message = f"{user_message}\n\n---\n\n[COMPACTION RECOVERY]\n{recovery}"
                logger.info("Compaction recovery injected")

        # Session resume context: notify LLM that this is a resumed conversation
        if self._conv_manager.is_restored(self._current_user_id):
            n_msgs = len(messages)
            if n_msgs > 0:
                user_message = (
                    f"{user_message}\n\n---\n"
                    f"[SESSION RESUMED — {n_msgs} previous messages restored from last session. "
                    f"Continue the conversation naturally without mentioning the restore.]"
                )
                logger.info("Session resume context injected (%d messages)", n_msgs)
            self._conv_manager.clear_restored_flag(self._current_user_id)

        # Add user message to conversation (with images if present)
        if images:
            content: list[dict] = [{"type": "text", "text": user_message}]
            content.extend(images)
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user_message})

        # Log to session (with user_id for replay filtering)
        self._last_user_msg_id = self.session.log_user_message(
            user_message, user_id=self._current_user_id,
        )
        return user_message

    async def _prepare_iteration(
        self, messages: list[dict], user_id: str | None, *,
        cached_system: str | None = None,
        cached_tool_defs: list[dict] | None = None,
        user_message: str = "",
        turn_prompt_override: str | None = None,
    ) -> tuple[list[dict], str, list[dict]]:
        """Shared per-iteration prep: compaction, repair, build prompt/tools.

        Pass cached_system/cached_tool_defs to reuse from the first iteration
        (system prompt and tool defs don't change within a single turn).
        user_message is used for lazy tool loading on the first iteration.

        Returns (messages, system_prompt, tool_defs).
        """
        # Tier 1: Snip old tool results (fast, no LLM)
        if self.context.needs_snip() and not self.context.needs_compaction():
            messages, freed = self.context.snip_messages(messages)
            if freed > 0:
                self._conv_manager.set_messages(user_id, messages)
                logger.info("Snipped old tool results, freed ~%d tokens", freed)

        # Tier 2: LLM summarization compaction
        if self.context.needs_compaction() and len(messages) > 6:
            # Memory flush: save durable memories BEFORE context is lost
            await memory_flush(
                messages, self.provider, self.tools,
                self._build_system_prompt, self._build_assistant_tool_message,
            )
            summary = await summarize_for_compaction(
                messages, self.provider, self.config, self.context,
            )
            compacted = self.context.compact_messages(messages, summary_text=summary)
            self._conv_manager.set_messages(user_id, compacted)
            messages = compacted
            logger.info("Proactive compaction triggered at %.1f%% (mode=%s)",
                       self.context.get_context_percent(), self.config.compaction_mode)

        # Repair messages only on the first iteration (cached_system is None)
        if cached_system is None:
            messages = strip_thinking_blocks(messages)
            messages = repair_messages(messages)
            self._conv_manager.set_messages(user_id, messages)

        if cached_system is not None:
            system = cached_system
        else:
            # Match skills on the first iteration only
            active_skills_content = ""
            if self._skills and user_message:
                from qanot.skills import match_skills, format_active_skills
                matched = match_skills(self._skills, user_message)
                if matched:
                    active_skills_content = format_active_skills(matched)
            system = self._build_system_prompt(active_skills_content=active_skills_content, turn_prompt_override=turn_prompt_override)
        # Lazy tool loading: only send tools the user likely needs
        if cached_tool_defs is not None:
            tool_defs = cached_tool_defs
        else:
            tool_defs = self.tools.get_lazy_definitions(user_message)
        return messages, system, tool_defs

    async def _handle_overflow(self, messages: list[dict], user_id: str | None) -> list[dict]:
        """Handle context overflow by force-compacting the conversation."""
        return await handle_overflow(
            messages, self.provider, self.tools,
            self.config, self.context, self._conv_manager, user_id,
            self._build_system_prompt, self._build_assistant_tool_message,
        )

    async def _init_turn(
        self, user_message: str, user_id: str | None, *, images: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        """Shared pre-loop setup for both streaming and non-streaming turns.

        Returns (possibly modified user_message, messages list).
        """
        self._current_user_id = user_id or ""
        self.context.turn_count += 1
        if user_id:
            self.cost_tracker.add_turn(user_id)
        messages = self._get_messages(user_id)
        user_message = await self._prepare_turn(user_message, messages, images=images)

        # Lifecycle hooks: pre-turn
        modified = await self.hooks.fire("on_pre_turn", user_id=user_id or "", message=user_message)
        if modified is not None:
            user_message = modified

        return user_message, messages

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
                final_text = f"{final_text}\n\n---\n\u26a0\ufe0f {warning}"

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

    async def run_turn(
        self,
        user_message: str,
        user_id: str | None = None,
        images: list[dict] | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
        system_prompt_override: str | None = None,
    ) -> str:
        """Process a user message through the agent loop.

        Args:
            user_message: The user's text input.
            user_id: Unique user identifier for conversation isolation.
            images: Optional list of Anthropic-style image blocks.
            chat_id: Telegram chat ID (for sub-agent result delivery).
            system_prompt_override: Per-turn system prompt override (thread-safe).

        Returns the final text response.
        """
        async with self._get_lock(user_id):
            self._current_chat_id = chat_id
            self._current_message_id = message_id
            # Budget enforcement: reject if daily limit exceeded
            if user_id and self.config.daily_budget_usd > 0:
                allowed, spent, budget = self.cost_tracker.check_budget(
                    str(user_id), self.config.daily_budget_usd,
                )
                if not allowed:
                    return (
                        f"Kunlik budget tugadi (${spent:.4f} / ${budget:.2f}). "
                        f"Ertaga qayta urinib ko'ring yoki admin bilan bog'laning."
                    )
            return await self._run_turn_impl(user_message, user_id, images=images, system_prompt_override=system_prompt_override)

    async def _run_turn_impl(self, user_message: str, user_id: str | None, *, images: list[dict] | None = None, system_prompt_override: str | None = None) -> str:
        """Internal implementation of run_turn (called under lock)."""
        user_message, messages = await self._init_turn(user_message, user_id, images=images)

        final_text = ""
        async for event in self._run_loop(user_message, user_id, messages, system_prompt_override=system_prompt_override, stream=False):
            if event.type == "done" and event.response:
                final_text = event.response.content or ""

        return final_text

    async def run_turn_stream(
        self,
        user_message: str,
        user_id: str | None = None,
        images: list[dict] | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
        system_prompt_override: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Process a user message with streaming.

        Yields StreamEvent objects as they arrive from the provider.
        The final event has type="done" with the complete ProviderResponse.
        Tool-use iterations are handled internally; text deltas from each
        iteration are yielded as they arrive.
        """
        async with self._get_lock(user_id):
            self._current_chat_id = chat_id
            self._current_message_id = message_id
            # Budget enforcement: reject if daily limit exceeded
            if user_id and self.config.daily_budget_usd > 0:
                allowed, spent, budget = self.cost_tracker.check_budget(
                    str(user_id), self.config.daily_budget_usd,
                )
                if not allowed:
                    msg = (
                        f"Kunlik budget tugadi (${spent:.4f} / ${budget:.2f}). "
                        f"Ertaga qayta urinib ko'ring yoki admin bilan bog'laning."
                    )
                    yield StreamEvent(type="done", response=ProviderResponse(content=msg))
                    return
            async for event in self._run_turn_stream_impl(user_message, user_id, images=images, system_prompt_override=system_prompt_override):
                yield event

    async def _run_turn_stream_impl(
        self, user_message: str, user_id: str | None, *, images: list[dict] | None = None, system_prompt_override: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        """Internal streaming implementation (called under lock)."""
        user_message, messages = await self._init_turn(user_message, user_id, images=images)

        async for event in self._run_loop(user_message, user_id, messages, system_prompt_override=system_prompt_override, stream=True):
            yield event

    def reset(self, user_id: str | None = None) -> None:
        """Reset conversation state for a user, or all if user_id is None."""
        if user_id is not None:
            self._remove_user_state(user_id)
        else:
            self._conv_manager.clear_all()

    # ── Snapshot persistence ──────────────────────────────

    def save_snapshot(self) -> int:
        """Save all active conversations to disk (call on shutdown).

        Returns number of conversations saved.
        """
        return self._conv_manager.save_snapshot(self.config.sessions_dir)

    def load_snapshot(self) -> int:
        """Load conversations from shutdown snapshot (call on startup).

        Returns number of conversations restored.
        """
        return self._conv_manager.load_snapshot(self.config.sessions_dir)

    def restore_user_session(self, user_id: str) -> int:
        """Explicitly restore a user's session from JSONL history.

        Returns the number of messages restored.
        Used by /resume command.
        """
        # Clear existing conversation first
        self._conv_manager.remove(user_id)
        # Force restore from JSONL
        messages = self._get_messages(user_id)
        return len(messages)


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
