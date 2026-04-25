"""Conversation state management — per-user history, locks, session restore.

Mixin that holds conversation-state plumbing for Agent.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class _ConversationMixin:
    """Per-user conversation history, locks, snapshot, session restore."""

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
