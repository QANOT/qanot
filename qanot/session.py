"""JSONL session read/write — compatible with LiveBuilder's activity_monitor."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import portalocker

from qanot.providers.base import Usage

logger = logging.getLogger(__name__)

# Default max turns to restore from session history
DEFAULT_HISTORY_LIMIT = 50


class SessionWriter:
    """Append-only JSONL session writer with file locking and replay."""

    def __init__(self, sessions_dir: str = "/data/sessions"):
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._session_id: str | None = None
        self._msg_counter = 0

    @property
    def session_id(self) -> str:
        """Return current session ID (date-based, lazily initialized)."""
        if self._session_id is None:
            self._session_id = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._session_id

    @property
    def session_path(self) -> Path:
        """Return the file path for the current session's JSONL file."""
        return self.sessions_dir / f"{self.session_id}.jsonl"

    def _next_id(self) -> str:
        self._msg_counter += 1
        return f"msg_{self._msg_counter:06d}"

    def _build_user_entry(self, text: str, parent_id: str, user_id: str) -> tuple[str, dict]:
        """Construct (msg_id, entry dict) for a user message without writing it."""
        msg_id = self._next_id()
        entry: dict = {
            "type": "message",
            "id": msg_id,
            "parentId": parent_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {
                "role": "user",
                "content": text,
            },
        }
        if user_id:
            entry["user_id"] = user_id
        return msg_id, entry

    def _build_assistant_entry(
        self,
        text: str,
        tool_uses: list[dict] | None,
        usage: Usage | None,
        parent_id: str,
        model: str,
        user_id: str,
    ) -> tuple[str, dict]:
        """Construct (msg_id, entry dict) for an assistant message without writing it."""
        msg_id = self._next_id()

        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        if tool_uses:
            for tu in tool_uses:
                block: dict = {
                    "type": "tool_use",
                    "name": tu.get("name", ""),
                    "input": tu.get("input", {}),
                }
                # Preserve Anthropic's tool_use id so session restore produces
                # a valid Claude history (id is required to match tool_result)
                if tu.get("id"):
                    block["id"] = tu["id"]
                content.append(block)

        entry: dict = {
            "type": "message",
            "id": msg_id,
            "parentId": parent_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {
                "role": "assistant",
                "content": content,
            },
        }

        if model:
            entry["model"] = model
        if user_id:
            entry["user_id"] = user_id

        if usage:
            entry["usage"] = {
                "input": usage.input_tokens,
                "output": usage.output_tokens,
                "cacheRead": usage.cache_read_input_tokens,
                "cacheWrite": usage.cache_creation_input_tokens,
                "cost": {"total": usage.cost},
            }

        return msg_id, entry

    def log_user_message(self, text: str, parent_id: str = "", user_id: str = "") -> str:
        """Log a user message synchronously. Returns the message ID."""
        msg_id, entry = self._build_user_entry(text, parent_id, user_id)
        self._append_sync(entry)
        return msg_id

    def log_assistant_message(
        self,
        text: str,
        tool_uses: list[dict] | None = None,
        usage: Usage | None = None,
        parent_id: str = "",
        model: str = "",
        user_id: str = "",
    ) -> str:
        """Log an assistant message synchronously. Returns the message ID."""
        msg_id, entry = self._build_assistant_entry(
            text, tool_uses, usage, parent_id, model, user_id,
        )
        self._append_sync(entry)
        return msg_id

    async def log_user_message_async(
        self, text: str, parent_id: str = "", user_id: str = "",
    ) -> str:
        """Async variant of log_user_message — does I/O off the event loop."""
        msg_id, entry = self._build_user_entry(text, parent_id, user_id)
        await self._append(entry)
        return msg_id

    async def log_assistant_message_async(
        self,
        text: str,
        tool_uses: list[dict] | None = None,
        usage: Usage | None = None,
        parent_id: str = "",
        model: str = "",
        user_id: str = "",
    ) -> str:
        """Async variant of log_assistant_message — does I/O off the event loop."""
        msg_id, entry = self._build_assistant_entry(
            text, tool_uses, usage, parent_id, model, user_id,
        )
        await self._append(entry)
        return msg_id

    async def _append(self, entry: dict) -> None:
        """Async append: offloads disk I/O to a thread to avoid blocking the loop."""
        await asyncio.to_thread(self._append_sync, entry)

    def _append_sync(self, entry: dict) -> None:
        """Append a JSON entry to the session file with cross-platform exclusive locking.

        Uses portalocker for Linux/macOS/Windows-uniform advisory locking.
        Holds the lock for the entire write+flush+fsync window so concurrent
        writers cannot produce torn JSONL lines, and so a crash mid-write
        cannot leave a partially-written line behind.
        """
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        # portalocker.Lock acquires an exclusive lock on entry and releases on exit.
        # mode="a" → append; LOCK_EX is blocking — fine here because session writes
        # are tiny and short-lived, and we'd rather queue than drop messages.
        with portalocker.Lock(
            str(self.session_path),
            mode="a",
            encoding="utf-8",
            flags=portalocker.LOCK_EX,
        ) as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync may legitimately fail on some filesystems (e.g. tmpfs
                # in containers, certain network mounts). The lock+flush still
                # guarantees no torn lines for concurrent writers; we just lose
                # the durability promise on a hard crash.
                logger.debug("fsync skipped for %s", self.session_path)

    def new_session(self, session_id: str | None = None) -> None:
        """Start a new session with an optional custom ID."""
        self._session_id = session_id
        self._msg_counter = 0

    # ── Session Replay ──

    def restore_history(
        self,
        user_id: str,
        max_turns: int = DEFAULT_HISTORY_LIMIT,
    ) -> list[dict]:
        """Restore conversation history for a user from JSONL session files.

        Reads recent session files, filters by user_id, extracts user/assistant
        message pairs, and returns the last `max_turns` user turns (each turn =
        user message + assistant response).

        Returns a list of message dicts ready to be used as conversation history.
        """
        raw_messages = self._read_user_messages(user_id)
        if not raw_messages:
            return []

        # Convert JSONL entries to conversation messages
        messages = _entries_to_messages(raw_messages)

        # Limit to last N user turns
        messages = _limit_history_turns(messages, max_turns)

        # Sanitize: remove orphaned tool results, fix broken pairs
        messages = _sanitize_restored_messages(messages)

        if messages:
            logger.info(
                "Restored %d messages for user %s from session history",
                len(messages), user_id,
            )

        return messages

    def _read_user_messages(self, user_id: str) -> list[dict]:
        """Read all JSONL entries for a specific user from recent session files."""
        if not isinstance(user_id, str):
            user_id = str(user_id)
        # Reject excessively long or empty user IDs
        if not user_id or len(user_id) > 256:
            logger.warning("Invalid user_id (empty or too long): %r", user_id[:50] if user_id else "")
            return []
        # Reject user IDs with control characters or path separators
        if any(c in user_id for c in ('\x00', '/', '\\', '..', '\n', '\r')):
            logger.warning("Rejected user_id with suspicious characters: %r", user_id[:50])
            return []

        entries: list[dict] = []

        # Read from recent session files (last 7 days)
        session_files = sorted(self.sessions_dir.glob("*.jsonl"), reverse=True)[:7]

        for filepath in session_files:
            try:
                with open(filepath, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # Validate entry is a dict with expected structure
                        if not isinstance(entry, dict):
                            continue
                        if entry.get("type") != "message":
                            continue
                        msg = entry.get("message")
                        if not isinstance(msg, dict):
                            continue
                        # Guard against excessively large content in restored history
                        content = msg.get("content", "")
                        if isinstance(content, str) and len(content) > 100_000:
                            logger.warning("Skipping oversized message in %s", filepath)
                            continue
                        elif isinstance(content, list):
                            total_len = 0
                            for b in content:
                                if isinstance(b, dict):
                                    total_len += len(b.get("text", ""))
                                    if total_len > 100_000:
                                        break
                            if total_len > 100_000:
                                logger.warning("Skipping oversized message in %s", filepath)
                                continue

                        # Filter by user_id.
                        # Legacy entries without user_id are included (personal bot
                        # — all messages belong to the same user).
                        entry_uid = entry.get("user_id", "")
                        if not isinstance(entry_uid, str):
                            entry_uid = str(entry_uid)
                        if entry_uid == user_id or not entry_uid:
                            entries.append(entry)
            except Exception as e:
                logger.warning("Failed to read session file %s: %s", filepath, e)

        # Sort by timestamp (oldest first)
        entries.sort(key=lambda e: e.get("timestamp", ""))
        return entries


def _extract_text_blocks(content: list) -> str:
    """Join text blocks from a structured content list, skipping non-text entries."""
    return "\n".join(
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _entries_to_messages(entries: list[dict]) -> list[dict]:
    """Convert JSONL session entries to conversation message format.

    Handles both simple text messages and structured content (tool_use blocks).
    Skips tool-use intermediate messages to keep history clean — only preserves
    user text messages and assistant final text responses.
    """
    messages: list[dict] = []

    for entry in entries:
        msg = entry.get("message", {})
        role = msg.get("role", "")
        content = msg.get("content")

        if not role or content is None:
            continue

        if role == "user":
            # User messages: keep text content only (skip tool_result blocks)
            if isinstance(content, str):
                # Strip RAG/compaction injection artifacts
                clean = _strip_injection(content)
                if clean:
                    messages.append({"role": "user", "content": clean})
            elif isinstance(content, list):
                # Structured content — extract text blocks only
                clean = _strip_injection(_extract_text_blocks(content))
                if clean:
                    messages.append({"role": "user", "content": clean})

        elif role == "assistant":
            if isinstance(content, str):
                if content:
                    messages.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                # Extract only text blocks from assistant (skip tool_use blocks)
                text = _extract_text_blocks(content)
                if text.strip():
                    messages.append({"role": "assistant", "content": text})

    return messages


def _strip_injection(text: str) -> str:
    """Strip RAG/compaction injection artifacts from restored user messages.

    These are added dynamically per-turn and should not be persisted in history.
    """
    for marker in ("\n\n---\n[MEMORY CONTEXT", "\n\n---\n\n[COMPACTION RECOVERY]"):
        if (idx := text.find(marker)) != -1:
            text = text[:idx]
    return text.strip()


def _limit_history_turns(messages: list[dict], max_turns: int) -> list[dict]:
    """Keep only the last N user turns from message history.

    A "turn" is a user message + the following assistant response.
    """
    if max_turns <= 0:
        return []

    # Walk backward counting user messages
    user_count = 0
    cut_index = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            user_count += 1
            if user_count > max_turns:
                cut_index = i + 1
                break

    return messages[cut_index:]


def _sanitize_restored_messages(messages: list[dict]) -> list[dict]:
    """Sanitize restored history to prevent API errors.

    Fixes:
    - Consecutive same-role messages (merge or keep last)
    - Empty messages
    - Ensures conversation starts with user message
    """
    if not messages:
        return messages

    # Remove empty messages
    messages = [m for m in messages if m.get("content")]

    # Ensure first message is from user
    while messages and messages[0].get("role") != "user":
        messages.pop(0)

    # Merge consecutive same-role messages
    sanitized: list[dict] = []
    for msg in messages:
        if sanitized and sanitized[-1].get("role") == msg.get("role"):
            # Merge: append text
            prev = sanitized[-1]
            if isinstance(prev["content"], str) and isinstance(msg["content"], str):
                prev["content"] += "\n" + msg["content"]
            else:
                sanitized.append(msg)  # Can't merge structured, just keep both
        else:
            sanitized.append(msg)

    # Ensure conversation ends properly (no trailing user without response)
    # This is fine — the next turn will add a new user message

    return sanitized
