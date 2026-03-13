"""Context management — token tracking, compaction, overflow prevention."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Safety margin: actual tokens can exceed estimates by ~20%
SAFETY_MARGIN = 1.2
# Compact when context exceeds this fraction of max
COMPACTION_THRESHOLD = 0.70
# After compaction, target this fraction
COMPACTION_TARGET = 0.40
# Working buffer activation threshold
BUFFER_THRESHOLD = 0.60
# Max chars to keep per tool result
MAX_TOOL_RESULT_CHARS = 8_000


def truncate_tool_result(result: str, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Truncate oversized tool results to prevent context bloat."""
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
        self.buffer_active = False
        self._buffer_started: str | None = None
        # Context size: last API call's input_tokens = actual context window usage
        self.last_prompt_tokens = 0

    @property
    def total_tokens(self) -> int:
        """Current context size: last prompt + all generated output."""
        return self.last_prompt_tokens + self.total_output

    def get_context_percent(self) -> float:
        """Get current context usage as a percentage.

        Uses last_prompt_tokens (actual context window usage from API).
        This is the real context size — NOT accumulated input tokens.
        """
        if self.max_tokens == 0:
            return 0.0
        return (self.last_prompt_tokens / self.max_tokens) * 100.0

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Record token usage from a provider response.

        input_tokens = full context sent to API (messages + system prompt).
        This is NOT additive — each call resends the full context.
        We track the latest value as current context size.
        """
        self.total_output += output_tokens
        self.api_calls += 1
        # input_tokens from API = actual context window size right now
        self.last_prompt_tokens = input_tokens
        # Increment turn count only on first call per user turn (not tool iterations)
        # Turn count is managed separately in agent.py

    def needs_compaction(self) -> bool:
        """Check if context needs proactive compaction before next API call.

        Returns True if estimated next-turn context would exceed threshold.
        """
        if self.max_tokens == 0:
            return False
        # Estimate next turn: current prompt + avg output per turn
        avg_output = self.total_output / max(self.turn_count, 1)
        estimated_next = self.last_prompt_tokens + avg_output
        # Apply safety margin for estimation error
        return (estimated_next * SAFETY_MARGIN) > (self.max_tokens * COMPACTION_THRESHOLD)

    def compact_messages(self, messages: list[dict], summary_text: str | None = None) -> list[dict]:
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
            summary_msg = {
                "role": "user",
                "content": (
                    f"[CONVERSATION SUMMARY — {removed_count} messages compacted]\n\n"
                    f"{summary_text}\n\n"
                    f"[End of summary. Recent conversation continues below.]"
                ),
            }
        else:
            # Fallback: simple truncation marker
            summary_msg = {
                "role": "user",
                "content": (
                    f"[CONTEXT COMPACTION: {removed_count} earlier messages were removed "
                    f"to free context space. Recent conversation preserved below. "
                    f"Check your workspace files (SESSION-STATE.md, memory/) for "
                    f"any important context from earlier in the conversation.]"
                ),
            }

        compacted = head + [summary_msg] + tail
        logger.info(
            "Compacted conversation: %d → %d messages (removed %d, summary=%s)",
            len(messages), len(compacted), removed_count, bool(summary_text),
        )

        # Reset prompt token estimate after compaction
        self.last_prompt_tokens = int(self.last_prompt_tokens * COMPACTION_TARGET / COMPACTION_THRESHOLD)

        return compacted

    @staticmethod
    def extract_compaction_text(messages: list[dict], keep_start: int = 2, keep_recent: int = 4) -> str:
        """Extract the text content of messages that would be removed during compaction.

        Returns a formatted string suitable for sending to an LLM for summarization.
        """
        if len(messages) <= keep_start + keep_recent:
            return ""

        middle = messages[keep_start:-keep_recent]
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
                            if len(result) > 200:
                                result = result[:200] + "..."
                            text_parts.append(f"[tool result: {result}]")
                text = "\n".join(text_parts)
            else:
                text = str(content)

            if text.strip():
                parts.append(f"**{role}**: {text[:500]}")

        return "\n\n".join(parts)

    def check_threshold(self) -> bool:
        """Check if we've crossed 60% context threshold.

        Returns True if we just crossed the threshold (first time).
        """
        pct = self.get_context_percent()
        if pct >= (BUFFER_THRESHOLD * 100) and not self.buffer_active:
            self.buffer_active = True
            self._buffer_started = datetime.now(timezone.utc).isoformat()
            self._init_working_buffer()
            return True
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
        import re
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
        """Append an exchange to the working buffer."""
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

        MAX_RECOVERY_FILE_CHARS = 20_000  # Limit per file to prevent context bloat

        parts = []
        for path, heading in sources:
            if path.exists():
                try:
                    # Check file size before reading to avoid loading huge files
                    file_size = path.stat().st_size
                    if file_size > MAX_RECOVERY_FILE_CHARS * 4:  # rough UTF-8 estimate
                        logger.warning(
                            "Recovery file %s is too large (%d bytes), truncating",
                            path, file_size,
                        )
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
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
