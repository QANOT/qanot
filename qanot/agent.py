"""Core agent loop — the heart of Qanot AI."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any, Callable, Awaitable

from qanot.config import Config
from qanot.context import ContextTracker, CostTracker, truncate_tool_result
from qanot.memory import wal_scan, wal_write, write_daily_note
from qanot.prompt import build_system_prompt
from qanot.providers.base import LLMProvider, ProviderResponse, StreamEvent, ToolCall, Usage
from qanot.providers.errors import (
    classify_error,
    PERMANENT_FAILURES,
    TRANSIENT_FAILURES,
    COMPACTION_FAILURES,
    ERROR_AUTH,
    ERROR_BILLING,
    ERROR_RATE_LIMIT,
    ERROR_CONTEXT_OVERFLOW,
)
from qanot.plugins.base import validate_tool_params
from qanot.session import SessionWriter

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 25
MAX_SAME_ACTION = 3  # Break after N identical consecutive tool calls
TOOL_TIMEOUT = 30  # seconds per tool execution
# Tools that run LLM agents internally need much longer timeouts
_LONG_RUNNING_TOOLS = frozenset({
    "delegate_to_agent",
    "converse_with_agent",
    "spawn_sub_agent",
})
LONG_TOOL_TIMEOUT = 300  # 5 minutes for delegation/conversation tools
CONVERSATION_TTL = 3600  # seconds before idle conversations are evicted
MAX_COMPACTION_RETRIES = 2  # Max overflow→compact→retry cycles

COMPACTION_SUMMARY_PROMPT = (
    "You are summarizing a conversation for context compaction. "
    "Create a concise summary that preserves:\n"
    "1. **Key decisions** made during the conversation\n"
    "2. **Open tasks/TODOs** that are still pending\n"
    "3. **Important facts** (names, numbers, IDs, URLs, file paths)\n"
    "4. **User preferences** expressed during the conversation\n"
    "5. **Current goal** — what the user is trying to accomplish\n\n"
    "Be concise but preserve all actionable information. "
    "Do NOT add commentary — just summarize the facts.\n\n"
    "---\n\n"
    "Conversation to summarize:\n\n"
)

MEMORY_FLUSH_PROMPT = (
    "Pre-compaction memory flush. Context is about to be compacted and older messages will be lost.\n\n"
    "Save any durable memories to files using write_file tool:\n"
    "- Save to `memory/{date}.md` (append, don't overwrite) for daily logs\n"
    "- Save to `MEMORY.md` for long-term curated facts\n\n"
    "What to save:\n"
    "- User's name, preferences, important personal info\n"
    "- Decisions made during this conversation\n"
    "- Project context, URLs, IDs, paths that might be needed later\n"
    "- Lessons learned, mistakes to avoid\n"
    "- Things the user explicitly asked to remember\n\n"
    "What NOT to save:\n"
    "- Routine greetings or small talk\n"
    "- Information already in MEMORY.md or USER.md\n"
    "- Temporary debugging details\n\n"
    "If nothing worth saving, reply with just: NO_SAVE\n"
    "Be concise. Append to existing files, never overwrite."
)

# Only allow read/write tools during memory flush (no shell, no web)
MEMORY_FLUSH_TOOL_NAMES = {"read_file", "write_file", "list_files", "memory_search"}

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
    return hashlib.sha256(raw.encode()).hexdigest()


def _result_fingerprint(result: str) -> str:
    """Hash a tool result for no-progress detection."""
    return hashlib.sha256(result.encode()).hexdigest()[:16]


_VERBOSE_KEYS = frozenset({"details", "debug", "trace", "raw", "stacktrace", "verbose", "raw_response"})


def _strip_verbose_result(result: str) -> str:
    """Strip verbose fields from tool results to save context tokens.

    Removes common bloat fields like 'details', 'debug', 'trace', 'raw'
    from JSON results while preserving the core data.
    """
    try:
        data = json.loads(result)
        if not isinstance(data, dict):
            return result
        stripped = False
        for key in _VERBOSE_KEYS:
            if key in data:
                val = data[key]
                if isinstance(val, str) and len(val) > 200:
                    data[key] = val[:100] + f"... [{len(val)} chars stripped]"
                    stripped = True
                elif isinstance(val, (list, dict)) and len(json.dumps(val)) > 500:
                    data[key] = f"[{type(val).__name__} with {len(val)} items stripped]"
                    stripped = True
        return json.dumps(data) if stripped else result
    except (json.JSONDecodeError, TypeError):
        return result


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


def _is_no_progress(result_history: list[tuple[str, str]], call_key: str, result_hash: str) -> bool:
    """Detect no-progress: same call producing same result repeatedly.

    result_history: list of (call_fingerprint, result_hash) tuples.
    Returns True if the same call+result pair has occurred 2+ times already.
    """
    pair = (call_key, result_hash)
    same_count = sum(1 for entry in result_history if entry == pair)
    return same_count >= 2


def _strip_old_images(messages: list[dict]) -> list[dict]:
    """Strip base64 image blocks from all user messages except the last one.

    Images are huge (~130K+ chars each) and bloat context fast.
    Once the model has seen and responded to an image, the base64 data
    is no longer needed — replace with a lightweight placeholder.
    """
    if not messages:
        return messages

    # Find the index of the last user message that contains images
    last_image_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            if any(
                isinstance(b, dict) and b.get("type") == "image"
                for b in msg["content"]
            ):
                last_image_idx = i
                break

    if last_image_idx < 0:
        return messages

    result = []
    for i, msg in enumerate(messages):
        if (
            i != last_image_idx
            and msg.get("role") == "user"
            and isinstance(msg.get("content"), list)
        ):
            # Replace image blocks with placeholder text
            new_content = []
            image_count = 0
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "image":
                    image_count += 1
                else:
                    new_content.append(block)
            if image_count:
                new_content.append({
                    "type": "text",
                    "text": f"[{image_count} image(s) were analyzed in this turn]",
                })
            result.append({"role": msg["role"], "content": new_content})
        else:
            result.append(msg)

    return result


def _strip_thinking_blocks(messages: list[dict]) -> list[dict]:
    """Strip thinking blocks from assistant messages in conversation history.

    Thinking blocks are internal reasoning from the model (extended thinking).
    They must not be sent back in context — the API rejects them and they
    waste tokens. Like OpenClaw's dropThinkingBlocks().
    """
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        filtered = [
            block for block in content
            if not (isinstance(block, dict) and block.get("type") == "thinking")
        ]
        if len(filtered) != len(content):
            msg["content"] = filtered if filtered else [{"type": "text", "text": ""}]
    return messages


def _repair_messages(messages: list[dict]) -> list[dict]:
    """Repair message history to fix common corruption issues.

    Fixes:
    - Orphaned tool_result blocks (no matching tool_use)
    - Consecutive same-role messages (merge or remove)
    - Base64 image bloat in older messages
    """
    if not messages:
        return messages

    # Strip old images first to prevent context bloat
    messages = _strip_old_images(messages)

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
    """Registry of available tools with lazy loading support.

    Tools are grouped by category. Core tools (always loaded) are sent
    with every API call. Extended tools are only sent when relevant,
    saving tokens on every request.
    """

    # Core tools: always sent to LLM (cheap, frequently used)
    CORE_CATEGORY = "core"
    # Extended: only loaded when the user's message hints they're needed
    EXTENDED_CATEGORIES = {"rag", "image", "web", "cron", "agent", "plugin"}

    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Callable[[dict], Awaitable[str]]] = {}
        self._categories: dict[str, str] = {}  # tool_name → category
        self._cached_definitions: list[dict] | None = None
        self._cached_core: list[dict] | None = None

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable[[dict], Awaitable[str]],
        category: str = "core",
    ) -> None:
        """Register a tool with its handler.

        Args:
            category: Tool category for lazy loading.
                "core" = always loaded (read_file, write_file, etc.)
                "rag", "image", "web", "cron", "agent", "plugin" = loaded on demand.
        """
        if name in self._tools:
            logger.warning("Tool '%s' already registered — overriding", name)
        self._tools[name] = {
            "name": name,
            "description": description,
            "input_schema": parameters,
        }
        self._handlers[name] = handler
        self._categories[name] = category
        self._cached_definitions = None
        self._cached_core = None

    def get_definitions(self) -> list[dict]:
        """Get ALL tool definitions (fallback, full list)."""
        if self._cached_definitions is None:
            self._cached_definitions = list(self._tools.values())
        return self._cached_definitions

    def get_lazy_definitions(self, user_message: str = "") -> list[dict]:
        """Get tool definitions — returns ALL tools every time.

        Why not filter? Because Ollama (and most providers) cache the KV state
        when the prompt prefix is identical. Sending the same tools every time
        means prompt_eval is near-zero on subsequent calls (cache hit).

        Changing the tool set per message BREAKS the cache and causes
        full prompt re-evaluation every time — much slower.

        OpenClaw uses the same strategy: consistent tool set = cache friendly.
        """
        return self.get_definitions()

    async def execute(self, name: str, input_data: dict, timeout: float = TOOL_TIMEOUT) -> str:
        """Execute a tool by name with parameter validation and timeout protection."""
        # Validate input types to prevent type confusion attacks
        if not isinstance(name, str) or not name.strip():
            return json.dumps({"error": "Invalid tool name"})
        # Sanitize tool name: must be alphanumeric/underscore, max 64 chars
        name = name.strip()
        if len(name) > 64 or not all(c.isalnum() or c == '_' for c in name):
            logger.warning("Rejected invalid tool name: %r", name[:80])
            return json.dumps({"error": "Invalid tool name: must be alphanumeric/underscore, max 64 chars"})
        if not isinstance(input_data, dict):
            logger.warning("Tool %s received non-dict input: %s", name, type(input_data).__name__)
            return json.dumps({"error": "Tool input must be a JSON object"})
        handler = self._handlers.get(name)
        if not handler:
            return json.dumps({"error": f"Unknown tool: {name}"})

        # Validate parameters against schema before execution
        tool_def = self._tools.get(name, {})
        schema = tool_def.get("input_schema", {})
        if schema:
            errors = validate_tool_params(input_data, schema)
            if errors:
                logger.warning("Tool %s param validation: %s", name, errors)
                return json.dumps({"error": f"Invalid parameters: {'; '.join(errors)}"})

        try:
            result = await asyncio.wait_for(handler(input_data), timeout=timeout)
            # Truncate oversized results
            return truncate_tool_result(result)
        except asyncio.TimeoutError:
            logger.error("Tool %s timed out after %ds", name, timeout)
            return json.dumps({"error": f"Tool timed out after {timeout}s"})
        except Exception as e:
            logger.error("Tool %s failed: %s", name, e, exc_info=True)
            # Sanitize error message to prevent leaking sensitive internals
            error_msg = str(e)
            # Truncate overly long error messages that may contain data dumps
            if len(error_msg) > 500:
                error_msg = error_msg[:500] + "... [truncated]"
            # Strip potential file system paths from error messages
            error_msg = re.sub(r'(/[\w./\-]+){3,}', '[path redacted]', error_msg)
            # Strip potential environment variable values or API keys
            error_msg = re.sub(r'(?:key|token|secret|password|auth)[=:]\s*\S+', '[credential redacted]', error_msg, flags=re.IGNORECASE)
            return json.dumps({"error": error_msg})

    def get_handler(self, name: str):
        """Get a tool handler by name. Returns None if not found."""
        return self._handlers.get(name)

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
        system_prompt_override: str = "",
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
        self._rag_indexer = None  # Set by main.py when RAG is enabled
        self.cost_tracker = CostTracker(config.workspace_dir)
        # Per-user conversation histories keyed by user_id.
        # None key is used for non-user contexts (cron jobs, etc.)
        self._conversations: dict[str | None, list[dict]] = {}
        self._locks: dict[str | None, asyncio.Lock] = {}
        self._last_active: dict[str | None, float] = {}
        self._last_user_msg_id = ""
        # Per-user pending images queue (populated by generate_image tool)
        self._pending_images: dict[str, list[str]] = {}
        # Per-user pending files queue (populated by send_file tool)
        self._pending_files: dict[str, list[str]] = {}
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

    @property
    def current_user_id(self) -> str:
        """Current user ID being processed (for RAG user-scoped queries)."""
        return self._current_user_id

    @property
    def current_chat_id(self) -> int | None:
        """Current Telegram chat ID being processed (for sub-agent delivery)."""
        return self._current_chat_id

    def get_conversation(self, user_id: str | None) -> list[dict]:
        """Get conversation history for a user (read-only view)."""
        return self._conversations.get(user_id, [])

    def _get_lock(self, user_id: str | None) -> asyncio.Lock:
        """Get or create a per-user lock for write safety."""
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    def _remove_user_state(self, user_id: str | None) -> None:
        """Remove all per-user state (conversation, lock, activity timestamp)."""
        self._conversations.pop(user_id, None)
        self._locks.pop(user_id, None)
        self._last_active.pop(user_id, None)

    def _evict_stale(self) -> None:
        """Remove conversation state for users idle longer than CONVERSATION_TTL."""
        now = time.monotonic()
        stale = [
            uid for uid, ts in self._last_active.items()
            if now - ts > CONVERSATION_TTL
        ]
        for uid in stale:
            self._remove_user_state(uid)
            logger.debug("Evicted stale conversation for user_id=%s", uid)

    def _get_messages(self, user_id: str | None = None) -> list[dict]:
        """Get or create conversation history for a user.

        On first access for a user (after restart or TTL eviction),
        restores recent history from JSONL session files so the bot
        remembers previous conversations.
        """
        self._evict_stale()
        self._last_active[user_id] = time.monotonic()
        if user_id not in self._conversations:
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
            self._conversations[user_id] = restored
        return self._conversations[user_id]

    def _build_system_prompt(self) -> str:
        """Build the system prompt from workspace files."""
        if self._system_prompt_override:
            return self._system_prompt_override
        return build_system_prompt(
            workspace_dir=self.config.workspace_dir,
            owner_name=self.config.owner_name,
            bot_name=self.config.bot_name,
            timezone_str=self.config.timezone,
            context_percent=self.context.get_context_percent(),
            total_tokens=self.context.total_tokens,
            mode=self.prompt_mode,
            user_id=str(self._current_user_id) if self._current_user_id else "",
        )

    async def _prepare_turn(self, user_message: str, messages: list[dict], *, images: list[dict] | None = None) -> str:
        """Shared turn setup: WAL scan, RAG context, compaction recovery, add user message.

        Returns the (possibly modified) user_message.
        """
        # WAL Protocol: scan user message BEFORE generating response
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

        # Link understanding: auto-fetch URLs in user messages
        if len(user_message.strip()) > 10:
            try:
                from qanot.links import fetch_link_previews

                link_context = await fetch_link_previews(user_message)
                if link_context:
                    user_message = f"{user_message}\n\n---\n{link_context}"
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

    async def _memory_flush(self, messages: list[dict]) -> None:
        """Run a hidden LLM turn to save durable memories before compaction.

        Like OpenClaw's pre-compaction flush: gives the agent a chance to
        write important facts to memory files before context is lost.
        Only read/write tools are available during flush.
        """
        if len(messages) < 4:
            return  # Too little context to flush

        # Build a condensed view of the conversation for the flush prompt
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        flush_prompt = MEMORY_FLUSH_PROMPT.replace("{date}", today)

        # Filter tools to only safe ones (read/write)
        flush_tools = [
            t for t in self.tools.get_definitions()
            if t.get("name") in MEMORY_FLUSH_TOOL_NAMES
        ]

        if not flush_tools:
            logger.warning("No flush tools available, skipping memory flush")
            return

        try:
            # Run up to 5 iterations (read existing → write new)
            flush_messages: list[dict] = list(messages)  # Copy current conversation
            flush_messages.append({"role": "user", "content": flush_prompt})

            for _ in range(5):
                response = await self.provider.chat(
                    messages=flush_messages,
                    tools=flush_tools,
                    system=self._build_system_prompt(),
                )

                if response.stop_reason == "tool_use" and response.tool_calls:
                    # Execute only allowed tools
                    flush_messages.append(
                        self._build_assistant_tool_message(response.content, response.tool_calls)
                    )
                    tool_results: list[dict] = []
                    for tc in response.tool_calls:
                        if tc.name in MEMORY_FLUSH_TOOL_NAMES:
                            result = await self.tools.execute(tc.name, tc.input)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tc.id,
                                "content": result,
                            })
                        else:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tc.id,
                                "content": "Tool not available during memory flush.",
                            })
                    flush_messages.append({"role": "user", "content": tool_results})
                else:
                    # end_turn or NO_SAVE — done
                    if response.content and "NO_SAVE" not in response.content:
                        logger.info("Memory flush completed with text response")
                    break

            logger.info("Pre-compaction memory flush completed")

        except Exception as e:
            logger.warning("Memory flush failed (non-fatal): %s", e)

    async def _summarize_for_compaction(self, messages: list[dict]) -> str | None:
        """Use multi-stage LLM summarization for compaction (OpenClaw-style).

        Returns summary text, or None if summarization fails (falls back to truncation).
        """
        from qanot.compaction import summarize_in_stages, estimate_messages_tokens

        if self.config.compaction_mode == "truncate":
            return None

        # Pre-compaction backup: save full context before it's dropped
        try:
            from pathlib import Path
            text_to_summarize = self.context.extract_compaction_text(messages)
            if text_to_summarize and len(text_to_summarize) > 100:
                backup_dir = Path(self.config.workspace_dir) / "memory"
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_path = backup_dir / f"pre-compact-{int(time.time())}.md"
                backup_path.write_text(
                    f"# Pre-Compaction Backup\n\n{text_to_summarize}",
                    encoding="utf-8",
                )
                logger.info("Pre-compaction backup saved: %s", backup_path.name)
        except Exception as e:
            logger.warning("Failed to save pre-compaction backup: %s", e)

        # Extract messages to summarize (middle section)
        if len(messages) <= 6:
            return None

        keep_recent = min(4, len(messages) // 2)
        middle = messages[2:-keep_recent]
        if not middle:
            return None

        total_tokens = estimate_messages_tokens(middle)
        logger.info(
            "Multi-stage compaction: %d messages (~%d tokens)",
            len(middle), total_tokens,
        )

        # Determine number of parts based on size
        parts = 2 if total_tokens < 50_000 else 3

        try:
            summary = await summarize_in_stages(
                provider=self.provider,
                messages=middle,
                context_window=self.config.max_context_tokens,
                parts=parts,
            )
            if summary and len(summary) > 20:
                logger.info("Multi-stage compaction summary: %d chars", len(summary))
                return summary
        except Exception as e:
            logger.warning("Multi-stage compaction failed, falling back to truncation: %s", e)

        return None

    async def _prepare_iteration(
        self, messages: list[dict], user_id: str | None, *,
        cached_system: str | None = None,
        cached_tool_defs: list[dict] | None = None,
        user_message: str = "",
    ) -> tuple[list[dict], str, list[dict]]:
        """Shared per-iteration prep: compaction, repair, build prompt/tools.

        Pass cached_system/cached_tool_defs to reuse from the first iteration
        (system prompt and tool defs don't change within a single turn).
        user_message is used for lazy tool loading on the first iteration.

        Returns (messages, system_prompt, tool_defs).
        """
        if self.context.needs_compaction() and len(messages) > 6:
            # Memory flush: save durable memories BEFORE context is lost
            await self._memory_flush(messages)
            summary = await self._summarize_for_compaction(messages)
            compacted = self.context.compact_messages(messages, summary_text=summary)
            self._conversations[user_id] = compacted
            messages = compacted
            logger.info("Proactive compaction triggered at %.1f%% (mode=%s)",
                       self.context.get_context_percent(), self.config.compaction_mode)

        # Repair messages only on the first iteration (cached_system is None)
        if cached_system is None:
            messages = _strip_thinking_blocks(messages)
            messages = _repair_messages(messages)
            self._conversations[user_id] = messages

        system = cached_system or self._build_system_prompt()
        # Lazy tool loading: only send tools the user likely needs
        if cached_tool_defs is not None:
            tool_defs = cached_tool_defs
        else:
            tool_defs = self.tools.get_lazy_definitions(user_message)
        return messages, system, tool_defs

    async def _handle_overflow(self, messages: list[dict], user_id: str | None) -> list[dict]:
        """Handle context overflow by force-compacting the conversation.

        Called reactively when the API returns a context_overflow error.
        """
        logger.warning("Context overflow detected — forcing compaction")
        await self._memory_flush(messages)
        summary = await self._summarize_for_compaction(messages)
        compacted = self.context.compact_messages(messages, summary_text=summary)
        self._conversations[user_id] = compacted
        return compacted

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
        batch_key = ":".join(sorted(_tool_call_fingerprint(tc.name, tc.input) for tc in tool_calls))

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
        except Exception:
            pass  # Non-fatal

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> tuple[list[dict], str]:
        """Execute tool calls and return (tool_result blocks, combined result hash)."""
        tool_results: list[dict] = []
        result_parts: list[str] = []
        for tc in tool_calls:
            logger.info("Executing tool: %s", tc.name)
            timeout = LONG_TOOL_TIMEOUT if tc.name in _LONG_RUNNING_TOOLS else TOOL_TIMEOUT
            result = await self.tools.execute(tc.name, tc.input, timeout=timeout)

            # Strip verbose detail fields from JSON results to save context
            result = _strip_verbose_result(result)

            if _is_deterministic_error(result):
                result_data = json.loads(result)
                result_data["_hint"] = "This error is permanent. Do not retry with the same parameters."
                result = json.dumps(result_data)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result,
            })
            result_parts.append(result)
        combined_hash = _result_fingerprint("|".join(result_parts))
        return tool_results, combined_hash

    def _handle_end_turn(
        self,
        final_text: str,
        user_message: str,
        messages: list[dict],
        usage: Usage,
    ) -> None:
        """Shared end-turn handling: append message, log, buffer, daily note."""
        messages.append({"role": "assistant", "content": final_text})
        # Persist per-user cost data
        self.cost_tracker.save()

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
    ) -> str:
        """Process a user message through the agent loop.

        Args:
            user_message: The user's text input.
            user_id: Unique user identifier for conversation isolation.
            images: Optional list of Anthropic-style image blocks.
            chat_id: Telegram chat ID (for sub-agent result delivery).

        Returns the final text response.
        """
        async with self._get_lock(user_id):
            self._current_chat_id = chat_id
            return await self._run_turn_impl(user_message, user_id, images=images)

    async def _run_turn_impl(self, user_message: str, user_id: str | None, *, images: list[dict] | None = None) -> str:
        """Internal implementation of run_turn (called under lock)."""
        self._current_user_id = user_id or ""
        self.context.turn_count += 1
        if user_id:
            self.cost_tracker.add_turn(user_id)
        messages = self._get_messages(user_id)
        user_message = await self._prepare_turn(user_message, messages, images=images)

        final_text = ""
        recent_fingerprints: list[str] = []
        result_history: list[tuple[str, str]] = []  # (call_hash, result_hash) for no-progress detection
        overflow_retries = 0
        cached_system: str | None = None
        cached_tool_defs: list[dict] | None = None

        for iteration in range(MAX_ITERATIONS):
            messages, system, tool_defs = await self._prepare_iteration(
                messages, user_id,
                cached_system=cached_system, cached_tool_defs=cached_tool_defs,
                user_message=user_message,
            )
            # Cache after first iteration — prompt/tools don't change within a turn
            if cached_system is None:
                cached_system = system
                cached_tool_defs = tool_defs

            try:
                response = await self._call_provider_with_retry(
                    messages=messages,
                    tools=tool_defs if tool_defs else None,
                    system=system,
                )
            except Exception as e:
                error_type = classify_error(e)
                logger.error("Provider failed after retries: %s [%s]", e, error_type)

                # Context overflow → compact and retry
                if error_type == ERROR_CONTEXT_OVERFLOW and overflow_retries < MAX_COMPACTION_RETRIES:
                    overflow_retries += 1
                    logger.info("Overflow recovery attempt %d/%d", overflow_retries, MAX_COMPACTION_RETRIES)
                    messages = await self._handle_overflow(messages, user_id)
                    continue

                # Log error for agent learning
                self._log_error_lesson(f"Provider error [{error_type}]", str(e))

                if error_type == ERROR_RATE_LIMIT:
                    return "Limitga yetdik, biroz kutib qaytadan urinib ko'ring."
                elif error_type == ERROR_AUTH:
                    return "API kalitda xatolik. Administrator bilan bog'laning."
                elif error_type == ERROR_BILLING:
                    return "API hisob muammosi. Administrator bilan bog'laning."
                elif error_type == ERROR_CONTEXT_OVERFLOW:
                    return "Suhbat juda uzun bo'lib qoldi. /reset buyrug'ini yuboring va qaytadan boshlang."
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

                tool_results, result_hash = await self._execute_tools(response.tool_calls)

                # No-progress detection: same call + same result = stuck
                batch_fps = [_tool_call_fingerprint(tc.name, tc.input) for tc in response.tool_calls]
                call_key = ":".join(sorted(batch_fps))
                if _is_no_progress(result_history, call_key, result_hash):
                    logger.warning("No-progress loop: same call producing same result")
                    self._log_error_lesson(
                        f"No-progress loop: {response.tool_calls[0].name}",
                        "Same call producing same result — need different approach",
                    )
                    final_text = (
                        f"Kechirasiz, {response.tool_calls[0].name} "
                        "bir xil natija qaytarmoqda. Boshqacha yondashuv kerak."
                    )
                    messages.append({"role": "assistant", "content": final_text})
                    break
                result_history.append((call_key, result_hash))

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
        self,
        user_message: str,
        user_id: str | None = None,
        images: list[dict] | None = None,
        chat_id: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Process a user message with streaming.

        Yields StreamEvent objects as they arrive from the provider.
        The final event has type="done" with the complete ProviderResponse.
        Tool-use iterations are handled internally; text deltas from each
        iteration are yielded as they arrive.
        """
        async with self._get_lock(user_id):
            self._current_chat_id = chat_id
            async for event in self._run_turn_stream_impl(user_message, user_id, images=images):
                yield event

    async def _run_turn_stream_impl(
        self, user_message: str, user_id: str | None, *, images: list[dict] | None = None
    ) -> AsyncIterator[StreamEvent]:
        """Internal streaming implementation (called under lock)."""
        self._current_user_id = user_id or ""
        self.context.turn_count += 1
        if user_id:
            self.cost_tracker.add_turn(user_id)
        messages = self._get_messages(user_id)
        user_message = await self._prepare_turn(user_message, messages, images=images)

        recent_fingerprints: list[str] = []
        result_history: list[tuple[str, str]] = []  # (call_hash, result_hash) for no-progress detection
        overflow_retries = 0
        cached_system: str | None = None
        cached_tool_defs: list[dict] | None = None

        for iteration in range(MAX_ITERATIONS):
            messages, system, tool_defs = await self._prepare_iteration(
                messages, user_id,
                cached_system=cached_system, cached_tool_defs=cached_tool_defs,
                user_message=user_message,
            )
            if cached_system is None:
                cached_system = system
                cached_tool_defs = tool_defs

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

                # Context overflow → compact and retry the iteration
                if error_type == ERROR_CONTEXT_OVERFLOW and overflow_retries < MAX_COMPACTION_RETRIES:
                    overflow_retries += 1
                    logger.info("Stream overflow recovery attempt %d/%d", overflow_retries, MAX_COMPACTION_RETRIES)
                    messages = await self._handle_overflow(messages, user_id)
                    continue

                # Try non-streaming fallback for transient errors
                if error_type in TRANSIENT_FAILURES:
                    await asyncio.sleep(3)
                    try:
                        response = await self.provider.chat(
                            messages=messages,
                            tools=tool_defs if tool_defs else None,
                            system=system,
                        )
                        # Calculate what text is new vs already streamed
                        already_streamed = "".join(text_parts)
                        if response.content:
                            new_text = response.content
                            if already_streamed and new_text.startswith(already_streamed):
                                # Only yield the part not yet streamed
                                remaining = new_text[len(already_streamed):]
                                if remaining:
                                    yield StreamEvent(type="text_delta", text=remaining)
                            elif not already_streamed:
                                yield StreamEvent(type="text_delta", text=new_text)
                            else:
                                # Partial stream doesn't match fallback — yield replacement marker
                                yield StreamEvent(type="text_delta", text="\n" + new_text)
                            text_parts = [response.content]  # Reset to full fallback content
                        tool_calls = response.tool_calls
                    except Exception:
                        yield StreamEvent(
                            type="done",
                            response=ProviderResponse(content="Xatolik yuz berdi, qaytadan urinib ko'ring."),
                        )
                        return
                elif error_type == ERROR_CONTEXT_OVERFLOW:
                    yield StreamEvent(
                        type="done",
                        response=ProviderResponse(
                            content="Suhbat juda uzun bo'lib qoldi. /reset buyrug'ini yuboring va qaytadan boshlang."
                        ),
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
                    messages.append({"role": "assistant", "content": loop_msg})
                    yield StreamEvent(
                        type="done",
                        response=ProviderResponse(content=loop_msg),
                    )
                    return

                text = response.content if response else ""
                usage = response.usage if response else Usage()

                messages.append(self._build_assistant_tool_message(text, tool_calls))
                self._log_tool_use(text, tool_calls, usage)

                tool_results, result_hash = await self._execute_tools(tool_calls)

                # No-progress detection: same call + same result = stuck
                batch_fps = [_tool_call_fingerprint(tc.name, tc.input) for tc in tool_calls]
                call_key = ":".join(sorted(batch_fps))
                if _is_no_progress(result_history, call_key, result_hash):
                    logger.warning("No-progress loop (stream): same call producing same result")
                    no_progress_msg = (
                        f"Kechirasiz, {tool_calls[0].name} "
                        "bir xil natija qaytarmoqda. Boshqacha yondashuv kerak."
                    )
                    messages.append({"role": "assistant", "content": no_progress_msg})
                    yield StreamEvent(
                        type="done",
                        response=ProviderResponse(content=no_progress_msg),
                    )
                    return
                result_history.append((call_key, result_hash))

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
            self._remove_user_state(user_id)
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
