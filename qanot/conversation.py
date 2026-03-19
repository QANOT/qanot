"""Per-user conversation management with history and locking."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Conversation:
    """A single user's conversation state."""

    messages: list[dict] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_active: float = field(default_factory=time.monotonic)


class ConversationManager:
    """Manages per-user conversations with history, locking, and cleanup."""

    def __init__(self, history_limit: int = 50, ttl: float = 3600.0):
        self._conversations: dict[str | None, Conversation] = {}
        self._history_limit = history_limit
        self._ttl = ttl

    def _get_or_create(self, user_id: str | None) -> Conversation:
        """Get existing conversation or create new one."""
        if user_id not in self._conversations:
            self._conversations[user_id] = Conversation()
        return self._conversations[user_id]

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
        """Remove conversation state for users idle longer than TTL."""
        now = time.monotonic()
        stale = [
            uid for uid, conv in self._conversations.items()
            if now - conv.last_active > self._ttl
        ]
        for uid in stale:
            self._conversations.pop(uid, None)
            logger.debug("Evicted stale conversation for user_id=%s", uid)

    def restore_from_session(
        self, user_id: str | None, messages: list[dict],
    ) -> list[dict]:
        """Restore messages from session history. Returns the stored list."""
        max_msgs = self._history_limit
        restored = messages[-max_msgs:]
        self.set_messages(user_id, restored)
        return self._conversations[user_id].messages
