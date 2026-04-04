"""Per-user conversation management with history, locking, and snapshot persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Conversation:
    """A single user's conversation state."""

    messages: list[dict] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_active: float = field(default_factory=time.monotonic)
    restored: bool = False  # True if conversation was restored from session/snapshot


class ConversationManager:
    """Manages per-user conversations with history, locking, and cleanup."""

    MAX_CONVERSATIONS = 500  # LRU cap — evict oldest idle when exceeded

    def __init__(self, history_limit: int = 50, ttl: float = 3600.0):
        self._conversations: dict[str | None, Conversation] = {}
        self._history_limit = history_limit
        self._ttl = ttl

    def _get_or_create(self, user_id: str | None) -> Conversation:
        """Get existing conversation or create new one (race-safe)."""
        conv = self._conversations.get(user_id)
        if conv is None:
            conv = self._conversations.setdefault(user_id, Conversation())
        return conv

    def get_messages(self, user_id: str | None) -> list[dict]:
        """Get message history for a user (empty list if none)."""
        conv = self._conversations.get(user_id)
        return conv.messages if conv else []

    def has_user(self, user_id: str | None) -> bool:
        """Check if a user has an active conversation."""
        return user_id in self._conversations

    def ensure_messages(self, user_id: str | None) -> list[dict]:
        """Get or create message history for a user, returning the live list.

        Callers can mutate the returned list directly (append, etc.).
        Also touches last_active timestamp.
        """
        conv = self._get_or_create(user_id)
        conv.last_active = time.monotonic()
        return conv.messages

    def set_messages(self, user_id: str | None, messages: list[dict]) -> None:
        """Replace entire message history for a user."""
        conv = self._get_or_create(user_id)
        conv.messages = messages

    def touch(self, user_id: str | None) -> None:
        """Update last-active timestamp for a user."""
        conv = self._conversations.get(user_id)
        if conv is not None:
            conv.last_active = time.monotonic()

    def remove(self, user_id: str | None) -> None:
        """Remove all state for a user."""
        self._conversations.pop(user_id, None)

    def clear_all(self) -> None:
        """Clear all conversations."""
        self._conversations.clear()

    def get_lock(self, user_id: str | None) -> asyncio.Lock:
        """Get the lock for a user's conversation."""
        return self._get_or_create(user_id).lock

    def active_count(self) -> int:
        """Return number of active conversations."""
        return len(self._conversations)

    def evict_stale(self) -> None:
        """Remove conversation state for users idle longer than TTL.

        Also enforces MAX_CONVERSATIONS cap via LRU eviction.
        """
        now = time.monotonic()
        stale = [
            uid for uid, conv in self._conversations.items()
            if now - conv.last_active > self._ttl
        ]
        for uid in stale:
            self._conversations.pop(uid, None)
            logger.debug("Evicted stale conversation for user_id=%s", uid)

        # LRU cap: evict oldest idle conversations when over limit
        overflow = len(self._conversations) - self.MAX_CONVERSATIONS
        if overflow > 0:
            by_age = sorted(
                self._conversations.items(),
                key=lambda kv: kv[1].last_active,
            )
            for uid, _ in by_age[:overflow]:
                self._conversations.pop(uid, None)
                logger.debug("LRU evicted conversation for user_id=%s", uid)

    def restore_from_session(
        self, user_id: str | None, messages: list[dict],
    ) -> list[dict]:
        """Restore messages from session history. Returns the stored list."""
        max_msgs = self._history_limit
        restored = messages[-max_msgs:]
        self.set_messages(user_id, restored)
        if restored:
            self._conversations[user_id].restored = True
        return self._conversations[user_id].messages

    def is_restored(self, user_id: str | None) -> bool:
        """Check if a user's conversation was restored from session/snapshot."""
        conv = self._conversations.get(user_id)
        return conv.restored if conv else False

    def clear_restored_flag(self, user_id: str | None) -> None:
        """Clear the restored flag after the first turn acknowledges it."""
        conv = self._conversations.get(user_id)
        if conv:
            conv.restored = False

    # ── Snapshot persistence ──────────────────────────────

    def save_snapshot(self, snapshot_dir: str) -> int:
        """Save all active conversations to a JSON snapshot file.

        Called on graceful shutdown to preserve conversation state.
        Returns the number of conversations saved.
        """
        if not self._conversations:
            return 0

        snapshot_path = Path(snapshot_dir) / "conversations_snapshot.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)

        data: dict[str, list[dict]] = {}
        for uid, conv in self._conversations.items():
            if uid is None or not conv.messages:
                continue
            # Only save last history_limit messages
            data[str(uid)] = conv.messages[-self._history_limit:]

        if not data:
            return 0

        try:
            # Atomic write via temp file
            tmp_path = snapshot_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp_path.replace(snapshot_path)
            logger.info("Saved conversation snapshot: %d users", len(data))
            return len(data)
        except Exception as e:
            logger.error("Failed to save conversation snapshot: %s", e)
            return 0

    def load_snapshot(self, snapshot_dir: str) -> int:
        """Load conversations from a snapshot file (called on startup).

        Returns the number of conversations restored.
        Snapshot file is deleted after successful load to prevent stale restores.
        """
        snapshot_path = Path(snapshot_dir) / "conversations_snapshot.json"
        if not snapshot_path.exists():
            return 0

        try:
            raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read conversation snapshot: %s", e)
            return 0

        if not isinstance(raw, dict):
            logger.warning("Invalid snapshot format (expected dict)")
            return 0

        count = 0
        for uid, messages in raw.items():
            if not isinstance(messages, list) or not messages:
                continue
            # Sanitize: keep only last history_limit messages
            trimmed = messages[-self._history_limit:]
            self.set_messages(uid, trimmed)
            self._conversations[uid].restored = True
            count += 1

        if count:
            logger.info("Restored %d conversations from snapshot", count)

        # Delete snapshot after load — it's a one-time restore
        try:
            snapshot_path.unlink()
        except OSError:
            pass

        return count
