"""Bot-to-bot loop prevention for group orchestration.

Prevents infinite message loops when multiple agent bots collaborate
in a Telegram group. Four independent guards must ALL pass:

1. Chain depth: tracks reply chain depth, caps at max_depth
2. Cooldown: per-bot minimum interval between responses
3. Dedup: SHA256 of (from_bot_id, text[:200]) — skip if seen recently
4. Chain timeout: total elapsed time since chain root message
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Maximum tracked messages for chain depth walking
MAX_TRACKED_MESSAGES = 1000

# Dedup window in seconds
DEDUP_WINDOW = 300  # 5 minutes


@dataclass
class MessageRecord:
    """Tracked message in the orchestration group."""

    message_id: int
    from_bot_id: int
    timestamp: float
    reply_to_message_id: int | None


class LoopGuard:
    """Prevents infinite bot-to-bot message loops in group orchestration.

    All state is in-memory only — restart clears it, which is correct
    behavior (stale loop state is worse than no state).
    """

    def __init__(
        self,
        max_depth: int = 5,
        cooldown_seconds: float = 2.0,
        chain_timeout_seconds: int = 300,
    ):
        self._max_depth = max_depth
        self._cooldown_seconds = cooldown_seconds
        self._chain_timeout = chain_timeout_seconds

        # message_id -> MessageRecord (bounded LRU)
        self._message_chain: OrderedDict[int, MessageRecord] = OrderedDict()

        # (chat_id, bot_id) -> last response timestamp
        self._last_response_time: dict[tuple[int, int], float] = {}

        # content_hash -> timestamp (for dedup)
        self._recent_hashes: dict[str, float] = {}

    def should_respond(self, message: Any, my_bot_id: int) -> tuple[bool, str]:
        """Check if a bot should respond to this message.

        Args:
            message: aiogram Message object (or compatible mock).
            my_bot_id: Telegram user ID of the bot considering responding.

        Returns:
            (allowed, reason_if_denied) tuple.
        """
        self._cleanup_expired()

        # 1. Human messages always pass
        from_user = getattr(message, "from_user", None)
        if not from_user or not getattr(from_user, "is_bot", False):
            return True, ""

        text = getattr(message, "text", None) or ""
        sender_id = getattr(from_user, "id", 0)
        chat_id = getattr(getattr(message, "chat", None), "id", 0)

        # 2. Check dedup: same sender + same content in last 5 min
        content_hash = self._hash_content(sender_id, text)
        if content_hash in self._recent_hashes:
            return False, f"dedup: identical message from bot {sender_id}"

        # 3. Check cooldown: did I respond in this chat within cooldown_seconds?
        last_response = self._last_response_time.get((chat_id, my_bot_id), 0)
        if time.time() - last_response < self._cooldown_seconds:
            return False, f"cooldown: {self._cooldown_seconds}s not elapsed"

        # 4. Check chain depth: walk reply chain, count bot messages
        depth = self._get_chain_depth(message)
        if depth >= self._max_depth:
            return False, f"depth: chain at {depth} >= max {self._max_depth}"

        # 5. Check chain timeout: is the root message older than chain_timeout?
        root_time = self._get_chain_root_time(message)
        if root_time and time.time() - root_time > self._chain_timeout:
            elapsed = time.time() - root_time
            return False, f"timeout: chain root is {elapsed:.0f}s old"

        return True, ""

    def track_response(self, message: Any, bot_id: int) -> None:
        """Record that a bot sent a response in the orchestration group.

        Call this AFTER successfully sending a message.

        Args:
            message: The sent aiogram Message object.
            bot_id: Telegram user ID of the bot that sent it.
        """
        message_id = getattr(message, "message_id", 0)
        chat_id = getattr(getattr(message, "chat", None), "id", 0)
        reply_to = getattr(message, "reply_to_message_id", None)
        # Some message objects nest reply_to in reply_to_message
        if reply_to is None:
            reply_msg = getattr(message, "reply_to_message", None)
            if reply_msg:
                reply_to = getattr(reply_msg, "message_id", None)

        now = time.time()

        # Track in chain
        if message_id:
            self._record_message(message_id, bot_id, now, reply_to)

        # Update cooldown
        if chat_id:
            self._last_response_time[(chat_id, bot_id)] = now

        # Track content hash for dedup
        text = getattr(message, "text", None) or ""
        content_hash = self._hash_content(bot_id, text)
        self._recent_hashes[content_hash] = now

    def track_incoming(self, message: Any) -> None:
        """Track an incoming message for chain depth calculations.

        Call this for every message seen in the orchestration group,
        regardless of whether we respond to it.
        """
        from_user = getattr(message, "from_user", None)
        if not from_user:
            return

        message_id = getattr(message, "message_id", 0)
        sender_id = getattr(from_user, "id", 0)
        reply_to = None
        reply_msg = getattr(message, "reply_to_message", None)
        if reply_msg:
            reply_to = getattr(reply_msg, "message_id", None)

        if message_id:
            self._record_message(
                message_id, sender_id, time.time(), reply_to,
            )

    def _get_chain_depth(self, message: Any) -> int:
        """Walk reply chain backwards, counting bot messages."""
        depth = 0
        reply_msg = getattr(message, "reply_to_message", None)
        if not reply_msg:
            return depth

        parent_id = getattr(reply_msg, "message_id", None)
        visited: set[int] = set()

        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            record = self._message_chain.get(parent_id)
            if not record:
                break
            # Count bot messages in chain (bot IDs are tracked)
            depth += 1
            parent_id = record.reply_to_message_id

        return depth

    def _get_chain_root_time(self, message: Any) -> float | None:
        """Find the timestamp of the root message in the reply chain."""
        reply_msg = getattr(message, "reply_to_message", None)
        if not reply_msg:
            return None

        parent_id = getattr(reply_msg, "message_id", None)
        root_time: float | None = None
        visited: set[int] = set()

        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            record = self._message_chain.get(parent_id)
            if not record:
                break
            root_time = record.timestamp
            if record.reply_to_message_id is None:
                break
            parent_id = record.reply_to_message_id

        return root_time

    def _record_message(
        self,
        message_id: int,
        from_bot_id: int,
        timestamp: float,
        reply_to_message_id: int | None,
    ) -> None:
        """Record a message in the chain tracker (bounded LRU)."""
        self._message_chain[message_id] = MessageRecord(
            message_id=message_id,
            from_bot_id=from_bot_id,
            timestamp=timestamp,
            reply_to_message_id=reply_to_message_id,
        )
        # Move to end (most recent)
        self._message_chain.move_to_end(message_id)

        # Evict oldest if over limit
        while len(self._message_chain) > MAX_TRACKED_MESSAGES:
            self._message_chain.popitem(last=False)

    def _cleanup_expired(self) -> None:
        """Remove expired dedup hashes and stale cooldown entries."""
        now = time.time()

        # Clean dedup hashes older than DEDUP_WINDOW
        expired_hashes = [
            h for h, ts in self._recent_hashes.items()
            if now - ts > DEDUP_WINDOW
        ]
        for h in expired_hashes:
            del self._recent_hashes[h]

        # Clean cooldown entries older than 10x cooldown (stale)
        stale_threshold = now - self._cooldown_seconds * 10
        stale_keys = [
            k for k, ts in self._last_response_time.items()
            if ts < stale_threshold
        ]
        for k in stale_keys:
            del self._last_response_time[k]

    @staticmethod
    def _hash_content(bot_id: int, text: str) -> str:
        """Generate content hash for dedup."""
        raw = f"{bot_id}:{text[:200]}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
