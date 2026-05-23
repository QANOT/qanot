"""Context management — token tracking, compaction, overflow prevention."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# Sentinel used for callers that don't pass a conv_key (legacy back-compat
# path, dashboard reads, single-tracker tests). Keeps the per-conv state
# dict from having a `None` key while preserving the old single-state
# semantics — every legacy call lands on the same slot.
_GLOBAL_SLOT = "__global__"


@dataclass
class _ConvState:
    """Per-conversation slot of state that USED to be global.

    ``last_prompt_tokens`` and ``buffer_active`` were single fields on
    ``ContextTracker`` and bled across threads — thread A reaching 90%
    made thread B's ``needs_compaction()`` see 90% on its next turn and
    spuriously compact a tiny conversation; thread A flipping
    ``buffer_active`` propagated to every other thread. Each conv_key
    now owns its own slot. Aggregates (``total_output``, ``api_calls``,
    ``turn_count``) intentionally stay global on the tracker — they're
    billing/operational counters, not per-thread state.
    """
    last_prompt_tokens: int = 0
    buffer_active: bool = False
    buffer_started: str | None = None

logger = logging.getLogger(__name__)

# Safety margin: actual tokens can exceed estimates by ~20%
SAFETY_MARGIN = 1.2
# Compact when context exceeds this fraction of max (proactive, before overflow)
COMPACTION_THRESHOLD = 0.60
# After compaction, target this fraction
COMPACTION_TARGET = 0.35
# Working buffer activation threshold (early warning)
BUFFER_THRESHOLD = 0.50
# Snip tier: strip old tool results at this threshold (before LLM compaction)
SNIP_THRESHOLD = 0.40
# Don't snip the last N messages (keep recent context intact)
SNIP_KEEP_RECENT = 6
# Max chars to keep per tool result
MAX_TOOL_RESULT_CHARS = 8_000
# Preview chars when persisting large tool results to disk
PERSIST_PREVIEW_CHARS = 2_000
# Max files to keep in .tool-results/ before cleanup
_MAX_TOOL_RESULT_FILES = 50
MAX_RECOVERY_FILE_CHARS = 20_000


def persist_tool_result(result: str, tool_name: str, workspace_dir: str) -> str:
    """Save a large tool result to disk and return a preview with file path.

    Creates {workspace_dir}/.tool-results/{tool_name}_{timestamp}.txt with the
    full result, then returns the first PERSIST_PREVIEW_CHARS chars plus a note
    pointing the model to the saved file.
    """
    results_dir = Path(workspace_dir) / ".tool-results"
    results_dir.mkdir(parents=True, exist_ok=True)

    timestamp = int(time.time())
    safe_name = tool_name or "unknown"
    filename = f"{safe_name}_{timestamp}.txt"
    filepath = results_dir / filename

    try:
        filepath.write_text(result, encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to persist tool result to %s: %s", filepath, exc)
        from qanot.utils import truncate_with_marker
        return truncate_with_marker(result, MAX_TOOL_RESULT_CHARS)

    _cleanup_old_results(results_dir)

    preview = result[:PERSIST_PREVIEW_CHARS]
    return (
        f"{preview}\n\n"
        f"[Full result ({len(result)} chars) saved to: .tool-results/{filename}]\n"
        f"[Use read_file to access the full result if needed]"
    )


def _cleanup_old_results(results_dir: Path) -> None:
    """Delete oldest files when .tool-results/ exceeds the file limit."""
    try:
        files = sorted(results_dir.iterdir(), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    excess = len(files) - _MAX_TOOL_RESULT_FILES
    if excess <= 0:
        return
    for f in files[:excess]:
        try:
            f.unlink()
        except OSError:
            pass


def truncate_tool_result(
    result: str,
    max_chars: int = MAX_TOOL_RESULT_CHARS,
    *,
    tool_name: str = "",
    workspace_dir: str = "",
) -> str:
    """Truncate oversized tool results to prevent context bloat.

    When workspace_dir is provided and the result exceeds max_chars, persists
    the full result to disk and returns a short preview with the file path.
    When workspace_dir is empty, falls back to in-memory truncation.
    """
    if len(result) <= max_chars:
        return result

    if workspace_dir:
        return persist_tool_result(result, tool_name, workspace_dir)

    from qanot.utils import truncate_with_marker
    return truncate_with_marker(result, max_chars)


class ContextTracker:
    """Track cumulative token usage and manage context thresholds."""

    def __init__(self, max_tokens: int = 200_000, workspace_dir: str = "/data/workspace"):
        self.max_tokens = max_tokens
        self.workspace_dir = Path(workspace_dir)
        # Billing: total output tokens generated (input is not additive — it's the same context resent)
        self.total_output = 0
        self.turn_count = 0
        self.api_calls = 0  # Total API calls (including tool loop iterations)
        # Per-conversation slots — the actual decision state. Each thread
        # writes its own slot via `add_usage(..., conv_key=...)`; each
        # decision (`needs_compaction(conv_key=...)`, `is_buffer_active`)
        # reads its own slot.
        self._per_conv: dict[str, _ConvState] = {}
        # Legacy "most recently active" view — kept as attributes so
        # dashboard/doctor/old tests don't have to thread a conv_key
        # through. Mirrors whichever slot wrote last via add_usage.
        self.buffer_active = False
        self._buffer_started: str | None = None
        # Context size: last API call's input_tokens = actual context window usage
        self.last_prompt_tokens = 0

    def _slot(self, conv_key: str | None) -> _ConvState:
        """Return the per-conv state slot (lazy-create on first touch).

        ``None`` falls back to a single shared "_global_" slot — that
        preserves the pre-per-thread API for any caller (tests, doctor)
        that hasn't been wired with a conv_key yet.
        """
        key = conv_key or _GLOBAL_SLOT
        slot = self._per_conv.get(key)
        if slot is None:
            slot = _ConvState()
            self._per_conv[key] = slot
        return slot

    @property
    def total_tokens(self) -> int:
        """Current context size: last prompt + all generated output."""
        return self.last_prompt_tokens + self.total_output

    def get_context_percent(self, conv_key: str | None = None) -> float:
        """Get this conversation's current context usage as a percentage.

        Uses the per-conv slot's ``last_prompt_tokens`` so a freshly
        started thread isn't blamed for a busy thread's recent call.
        Falls back to the legacy "most recently active" view when
        ``conv_key`` is None (back-compat for doctor/dashboard).
        """
        if self.max_tokens == 0:
            return 0.0
        tokens = (
            self._slot(conv_key).last_prompt_tokens
            if conv_key is not None else self.last_prompt_tokens
        )
        return (tokens / self.max_tokens) * 100.0

    def is_buffer_active(self, conv_key: str | None = None) -> bool:
        """Per-conv buffer-mode read.

        Replaces the legacy ``tracker.buffer_active`` attribute access
        for callers that hold a conv_key (the agent loop). The
        attribute still exists for unscoped readers and reflects the
        most recently active conv.
        """
        if conv_key is None:
            return self.buffer_active
        return self._slot(conv_key).buffer_active

    def add_usage(
        self, input_tokens: int, output_tokens: int,
        conv_key: str | None = None,
    ) -> None:
        """Record token usage from a provider response.

        ``input_tokens`` = full context sent to API (messages + system
        prompt). NOT additive — each call resends the full context. We
        track the latest value per-conv (so thread A's 90% doesn't leak
        into thread B's needs_compaction() check on its next turn).

        Buffer activation is driven by ``check_threshold(conv_key)``,
        which the caller invokes right after this — keeping the
        "just activated" return semantics intact.
        """
        self.total_output += output_tokens
        self.api_calls += 1
        # Per-conv slot is the authoritative state for decisions.
        self._slot(conv_key).last_prompt_tokens = input_tokens
        # Mirror "most recently active" tokens to the legacy attribute
        # so the dashboard / doctor / old tests reading
        # ``tracker.last_prompt_tokens`` see the freshest conv's view
        # without needing a conv_key.
        self.last_prompt_tokens = input_tokens
        # Increment turn count only on first call per user turn (not tool iterations)
        # Turn count is managed separately in agent.py

    def needs_compaction(self, conv_key: str | None = None) -> bool:
        """Check if THIS conversation needs proactive compaction.

        Uses the per-conv slot so thread A finishing at 95% doesn't make
        thread B's next-turn check fire spuriously on a 5K conversation.
        ``avg_output`` stays global — it's a per-agent estimator and
        an OK approximation for any conversation.
        """
        if self.max_tokens == 0:
            return False
        slot_tokens = (
            self._slot(conv_key).last_prompt_tokens
            if conv_key is not None else self.last_prompt_tokens
        )
        avg_output = self.total_output / max(self.turn_count, 1)
        return ((slot_tokens + avg_output) * SAFETY_MARGIN) > (self.max_tokens * COMPACTION_THRESHOLD)

    def needs_snip(self, conv_key: str | None = None) -> bool:
        """Check if this conv needs snipping (strip old tool results)."""
        if self.max_tokens == 0:
            return False
        slot_tokens = (
            self._slot(conv_key).last_prompt_tokens
            if conv_key is not None else self.last_prompt_tokens
        )
        return (slot_tokens / self.max_tokens) > SNIP_THRESHOLD

    def snip_messages(self, messages: list[dict]) -> tuple[list[dict], int]:
        """Strip verbose tool results from old messages to free context.

        Returns (snipped_messages, tokens_freed_estimate).
        This is a fast, no-LLM operation — the first tier of compaction.
        Does not mutate the original messages.
        """
        if len(messages) <= SNIP_KEEP_RECENT:
            return messages, 0

        cutoff = len(messages) - SNIP_KEEP_RECENT
        chars_freed = 0
        result: list[dict] = []

        for i, msg in enumerate(messages):
            if i >= cutoff:
                # Recent messages — keep as-is
                result.append(msg)
                continue

            content = msg.get("content")
            if msg.get("role") != "user" or not isinstance(content, list):
                result.append(msg)
                continue

            # Scan content blocks for tool_result entries worth snipping
            new_blocks: list[dict] | None = None
            for j, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue

                inner = block.get("content", "")
                # Handle nested content blocks inside tool_result
                if isinstance(inner, list):
                    text_parts = [
                        b.get("text", "")
                        for b in inner
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    inner_text = "\n".join(text_parts)
                else:
                    inner_text = str(inner)

                if len(inner_text) <= 500:
                    continue

                # Worth snipping — lazily copy blocks list
                if new_blocks is None:
                    new_blocks = list(content)
                original_len = len(inner_text)
                chars_freed += original_len
                snipped_content = f"[tool result snipped — {original_len} chars]"
                new_block = dict(block)
                new_block["content"] = snipped_content
                new_blocks[j] = new_block

            if new_blocks is not None:
                new_msg = dict(msg)
                new_msg["content"] = new_blocks
                result.append(new_msg)
            else:
                result.append(msg)

        tokens_freed = chars_freed // 4
        return result, tokens_freed

    def compact_messages(
        self, messages: list[dict], summary_text: str | None = None,
        conv_key: str | None = None,
    ) -> list[dict]:
        """Compact conversation history to reduce context usage.

        Args:
            messages: Full message history.
            summary_text: If provided, use this LLM-generated summary instead
                of a simple truncation marker. When None, falls back to
                truncation-only mode.

        Strategy:
        - Keep first 2 messages (initial context) + last 4 (recent turns)
        - Replace the middle with either an LLM summary or a truncation marker
        """
        if len(messages) <= 6:
            return messages  # Too few to compact

        # Keep first 2 (initial context) + last 4 (recent context)
        keep_recent = min(4, len(messages) // 2)
        keep_start = 2

        head = messages[:keep_start]
        tail = messages[-keep_recent:]
        removed_count = len(messages) - keep_start - keep_recent

        if summary_text:
            # LLM-generated summary
            summary_content = (
                f"[CONVERSATION SUMMARY — {removed_count} messages compacted]\n\n"
                f"{summary_text}\n\n"
                f"[End of summary. Recent conversation continues below.]"
            )
        else:
            # Fallback: simple truncation marker
            summary_content = (
                f"[CONTEXT COMPACTION: {removed_count} earlier messages were removed "
                f"to free context space. Recent conversation preserved below. "
                f"Check your workspace files (SESSION-STATE.md, memory/) for "
                f"any important context from earlier in the conversation.]"
            )
        summary_msg = {"role": "user", "content": summary_content}

        compacted = head + [summary_msg] + tail
        logger.info(
            "Compacted conversation: %d → %d messages (removed %d, summary=%s)",
            len(messages), len(compacted), removed_count, bool(summary_text),
        )

        # Reset prompt token estimate after compaction to the target
        # fraction of max — on the PER-CONV slot for this conversation,
        # and mirror to the legacy attribute for unscoped readers.
        reset_to = int(self.max_tokens * COMPACTION_TARGET)
        self._slot(conv_key).last_prompt_tokens = reset_to
        self.last_prompt_tokens = reset_to

        return compacted

    @staticmethod
    def extract_compaction_text(messages: list[dict], keep_start: int = 2, keep_recent: int | None = None) -> str:
        """Extract the text content of messages that would be removed during compaction.

        Returns a formatted string suitable for sending to an LLM for summarization.
        Uses the same keep_recent logic as compact_messages to ensure consistency.
        """
        if keep_recent is None:
            keep_recent = min(4, len(messages) // 2)
        if len(messages) <= keep_start + keep_recent:
            return ""

        middle = messages[keep_start:-keep_recent] if keep_recent > 0 else messages[keep_start:]
        parts: list[str] = []

        for msg in middle:
            role = msg.get("role", "?")
            content = msg.get("content", "")

            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # Extract text from content blocks, skip tool results
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            text_parts.append(f"[tool: {block.get('name', '?')}]")
                        elif block.get("type") == "tool_result":
                            # Truncate tool results to save tokens
                            result = block.get("content", "")
                            if isinstance(result, list):
                                result = " ".join(
                                    b.get("text", "") for b in result
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                            if len(result) > 200:
                                result = result[:200] + "..."
                            text_parts.append(f"[tool result: {result}]")
                text = "\n".join(text_parts)
            else:
                text = str(content)

            if text.strip():
                parts.append(f"**{role}**: {text[:500]}")

        return "\n\n".join(parts)

    def check_threshold(self, conv_key: str | None = None) -> bool:
        """Activate buffer mode for THIS conv if it just crossed the
        50% threshold; return True only on the first crossing.

        Per-conv so thread A flipping the flag doesn't make thread B
        also believe itself to be in buffer mode. The legacy
        ``self.buffer_active`` attribute mirrors the most recently
        active conv so old callers (dashboard, doctor) keep working.
        """
        pct = self.get_context_percent(conv_key=conv_key)
        slot = self._slot(conv_key)
        if pct >= (BUFFER_THRESHOLD * 100) and not slot.buffer_active:
            slot.buffer_active = True
            slot.buffer_started = datetime.now(timezone.utc).isoformat()
            # Mirror to legacy attributes for unscoped readers.
            self.buffer_active = True
            self._buffer_started = slot.buffer_started
            self._init_working_buffer()
            return True
        # Even on "already-active" path, refresh the mirror so the
        # legacy view always reflects the most recently active conv.
        self.buffer_active = slot.buffer_active
        self._buffer_started = slot.buffer_started
        return False

    def _init_working_buffer(self) -> None:
        """Initialize a fresh working buffer file."""
        buffer_path = self.workspace_dir / "memory" / "working-buffer.md"
        buffer_path.parent.mkdir(parents=True, exist_ok=True)

        content = (
            "# Working Buffer (Danger Zone Log)\n"
            f"**Status:** ACTIVE\n"
            f"**Started:** {self._buffer_started}\n"
            "\n---\n\n"
        )
        buffer_path.write_text(content, encoding="utf-8")
        logger.info("Working buffer initialized at %s", buffer_path)

    @staticmethod
    def _sanitize_buffer_content(text: str) -> str:
        """Sanitize text before writing to working buffer to prevent injection.

        Prevents users from injecting fake headers, agent summaries,
        or structural markers that could mislead recovery.
        """
        # Remove markdown headers that could fake structural elements
        sanitized = re.sub(r'^#{1,6}\s', '> ', text, flags=re.MULTILINE)
        # Remove horizontal rules that could fake section breaks
        sanitized = re.sub(r'^\s*-{3,}\s*$', '', sanitized, flags=re.MULTILINE)
        sanitized = re.sub(r'^\s*\*{3,}\s*$', '', sanitized, flags=re.MULTILINE)
        # Limit total length to prevent buffer flooding
        if len(sanitized) > 4000:
            sanitized = sanitized[:4000] + "\n[truncated]"
        return sanitized

    def append_to_buffer(self, human_msg: str, agent_summary: str) -> None:
        """Append a human/agent exchange to the working buffer for recovery."""
        if not self.buffer_active:
            return

        buffer_path = self.workspace_dir / "memory" / "working-buffer.md"
        if not buffer_path.exists():
            self._init_working_buffer()

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        safe_human = self._sanitize_buffer_content(human_msg)
        safe_summary = self._sanitize_buffer_content(agent_summary)
        entry = (
            f"\n## [{ts}] Human\n{safe_human}\n\n"
            f"## [{ts}] Agent (summary)\n{safe_summary}\n"
        )

        try:
            with open(buffer_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except OSError as exc:
            logger.warning("Failed to append to working buffer %s: %s", buffer_path, exc)

    _COMPACTION_MARKERS = (
        "<summary>", "truncated", "context limits",
        "context compaction", "where were we",
        "continue where", "what were we doing",
    )

    def detect_compaction(self, messages: list[dict]) -> bool:
        """Detect if we need compaction recovery.

        Checks for <summary> tags, truncation markers, or "where were we?" messages.
        """
        if not messages:
            return False

        markers = self._COMPACTION_MARKERS
        for msg in messages[:3]:  # Check first few messages
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                continue

            lower = text.lower()
            if any(marker in lower for marker in markers):
                return True

        return False

    def recover_from_compaction(self) -> str:
        """Read working buffer and session state for recovery.

        Returns recovery context string to inject into the session.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        sources = [
            (self.workspace_dir / "memory" / "working-buffer.md", "Working Buffer Recovery"),
            (self.workspace_dir / "SESSION-STATE.md", "Session State"),
            (self.workspace_dir / "memory" / f"{today}.md", "Today's Notes"),
        ]

        parts = []
        for path, heading in sources:
            if path.exists():
                try:
                    with path.open(encoding="utf-8", errors="replace") as fh:
                        content = fh.read(MAX_RECOVERY_FILE_CHARS + 1)
                    if len(content) > MAX_RECOVERY_FILE_CHARS:
                        logger.warning(
                            "Recovery file %s exceeds %d chars, truncating",
                            path, MAX_RECOVERY_FILE_CHARS,
                        )
                except OSError as exc:
                    logger.warning("Failed to read recovery file %s: %s", path, exc)
                    parts.append(f"## {heading}\n[Error reading file: {exc}]")
                    continue
                if content.strip():
                    if len(content) > MAX_RECOVERY_FILE_CHARS:
                        content = content[:MAX_RECOVERY_FILE_CHARS] + "\n[truncated]\n"
                    parts.append(f"## {heading}\n{content}")

        if parts:
            return "\n\n---\n\n".join(parts)
        return ""

    def session_status(self) -> dict:
        """Return current session status for the session_status tool."""
        return {
            "context_percent": round(self.get_context_percent(), 1),
            "context_tokens": self.last_prompt_tokens,
            "total_output_tokens": self.total_output,
            "total_tokens": self.total_tokens,
            "max_tokens": self.max_tokens,
            "buffer_active": self.buffer_active,
            "buffer_started": self._buffer_started,
            "turn_count": self.turn_count,
            "api_calls": self.api_calls,
        }


# CostTracker extracted to qanot/cost.py — re-exported here for backward compat
from qanot.cost import CostTracker  # noqa: F401
