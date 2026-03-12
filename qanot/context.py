"""Context management — token tracking, 60% detection, compaction recovery."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class ContextTracker:
    """Track cumulative token usage and manage context thresholds."""

    def __init__(self, max_tokens: int = 200_000, workspace_dir: str = "/data/workspace"):
        self.max_tokens = max_tokens
        self.workspace_dir = Path(workspace_dir)
        self.total_input = 0
        self.total_output = 0
        self.buffer_active = False
        self._buffer_started: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.total_input + self.total_output

    def get_context_percent(self) -> float:
        """Get current context usage as a percentage."""
        if self.max_tokens == 0:
            return 0.0
        return (self.total_input / self.max_tokens) * 100.0

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Record token usage from a provider response."""
        self.total_input += input_tokens
        self.total_output += output_tokens

    def check_threshold(self) -> bool:
        """Check if we've crossed 60% context threshold.

        Returns True if we just crossed the threshold (first time).
        """
        pct = self.get_context_percent()
        if pct >= 60.0 and not self.buffer_active:
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

    def append_to_buffer(self, human_msg: str, agent_summary: str) -> None:
        """Append an exchange to the working buffer."""
        if not self.buffer_active:
            return

        buffer_path = self.workspace_dir / "memory" / "working-buffer.md"
        if not buffer_path.exists():
            self._init_working_buffer()

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        entry = (
            f"\n## [{ts}] Human\n{human_msg}\n\n"
            f"## [{ts}] Agent (summary)\n{agent_summary}\n"
        )

        with open(buffer_path, "a", encoding="utf-8") as f:
            f.write(entry)

    def detect_compaction(self, messages: list[dict]) -> bool:
        """Detect if we need compaction recovery.

        Checks for <summary> tags, truncation markers, or "where were we?" messages.
        """
        if not messages:
            return False

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
            if any(marker in lower for marker in [
                "<summary>", "truncated", "context limits",
                "where were we", "continue where", "what were we doing",
            ]):
                return True

        return False

    def recover_from_compaction(self) -> str:
        """Read working buffer and session state for recovery.

        Returns recovery context string to inject into the session.
        """
        parts = []

        # Read working buffer
        buffer_path = self.workspace_dir / "memory" / "working-buffer.md"
        if buffer_path.exists():
            content = buffer_path.read_text(encoding="utf-8")
            if content.strip():
                parts.append(f"## Working Buffer Recovery\n{content}")

        # Read SESSION-STATE.md
        state_path = self.workspace_dir / "SESSION-STATE.md"
        if state_path.exists():
            content = state_path.read_text(encoding="utf-8")
            if content.strip():
                parts.append(f"## Session State\n{content}")

        # Read today's daily note
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_path = self.workspace_dir / "memory" / f"{today}.md"
        if daily_path.exists():
            content = daily_path.read_text(encoding="utf-8")
            if content.strip():
                parts.append(f"## Today's Notes\n{content}")

        if parts:
            return "\n\n---\n\n".join(parts)
        return ""

    def session_status(self) -> dict:
        """Return current session status for the session_status tool."""
        return {
            "context_percent": round(self.get_context_percent(), 1),
            "total_input_tokens": self.total_input,
            "total_output_tokens": self.total_output,
            "total_tokens": self.total_tokens,
            "max_tokens": self.max_tokens,
            "buffer_active": self.buffer_active,
            "buffer_started": self._buffer_started,
        }
