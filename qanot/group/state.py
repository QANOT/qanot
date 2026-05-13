"""Per-chat ephemeral state for zen-mode decisions.

Three time-windowed facts the classifier needs:
  1. **Last bot reply** — timestamp + the text the bot sent. Lets us
     detect "user is replying inside an active conversation with the
     bot" without scraping Telegram history.
  2. **Recent bot replies** — for rate limiting. Bounded deque of
     timestamps in the last 60 seconds.
  3. **Mute** — when does the silent state expire (set by
     "qanot mute" / "qanot jim tur" from any group member).

State is in-memory only. A restart clears it: the bot reverts to a
"fresh" worldview for each group, which is the safe default — better
to occasionally respond once after a restart than to silently honour
a stale mute the user has long since forgotten about.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque


@dataclass
class _LastReply:
    """What the bot last said in a group, when, and which message."""
    timestamp: float
    text: str
    message_id: int


@dataclass
class _ChatState:
    last_reply: _LastReply | None = None
    # Sliding-window deque of recent reply timestamps for rate limiting.
    # Bounded — old entries are popped when we record() / check().
    recent_reply_ts: Deque[float] = field(default_factory=deque)
    # Unix timestamp at/after which the bot is allowed to speak again.
    # 0 = no active mute.
    mute_until: float = 0.0


class GroupChatState:
    """Container for per-chat zen state. Thread-safe via plain dict
    access (Python's GIL covers the single-step assignments we do; the
    deque operations append/popleft are atomic on CPython).
    """

    def __init__(self) -> None:
        self._chats: dict[int, _ChatState] = {}

    def _get(self, chat_id: int) -> _ChatState:
        state = self._chats.get(chat_id)
        if state is None:
            state = _ChatState()
            self._chats[chat_id] = state
        return state

    # ── Bot replies ────────────────────────────────────────────────

    def record_bot_reply(
        self, chat_id: int, *, text: str, message_id: int,
        now: float | None = None,
    ) -> None:
        """Call this whenever the bot sends a final message into a
        group chat. Updates the 'last reply' fact and appends a
        timestamp to the rate-limit window.
        """
        ts = now if now is not None else time.time()
        state = self._get(chat_id)
        state.last_reply = _LastReply(
            timestamp=ts, text=text, message_id=message_id,
        )
        state.recent_reply_ts.append(ts)
        self._evict_old_window(state, ts)

    def last_reply(self, chat_id: int) -> _LastReply | None:
        state = self._chats.get(chat_id)
        return state.last_reply if state else None

    def seconds_since_last_reply(
        self, chat_id: int, now: float | None = None,
    ) -> float | None:
        last = self.last_reply(chat_id)
        if last is None:
            return None
        return (now if now is not None else time.time()) - last.timestamp

    # ── Rate limiting ──────────────────────────────────────────────

    def replies_in_last_minute(
        self, chat_id: int, now: float | None = None,
    ) -> int:
        state = self._chats.get(chat_id)
        if state is None:
            return 0
        ts = now if now is not None else time.time()
        self._evict_old_window(state, ts)
        return len(state.recent_reply_ts)

    @staticmethod
    def _evict_old_window(state: _ChatState, now: float) -> None:
        cutoff = now - 60.0
        while state.recent_reply_ts and state.recent_reply_ts[0] < cutoff:
            state.recent_reply_ts.popleft()

    # ── Mute ───────────────────────────────────────────────────────

    def mute(
        self, chat_id: int, *, minutes: int, now: float | None = None,
    ) -> None:
        """Silence the bot in this chat for ``minutes`` minutes."""
        ts = now if now is not None else time.time()
        self._get(chat_id).mute_until = ts + minutes * 60.0

    def unmute(self, chat_id: int) -> None:
        state = self._chats.get(chat_id)
        if state is not None:
            state.mute_until = 0.0

    def is_muted(self, chat_id: int, now: float | None = None) -> bool:
        state = self._chats.get(chat_id)
        if state is None or state.mute_until == 0.0:
            return False
        ts = now if now is not None else time.time()
        if ts >= state.mute_until:
            state.mute_until = 0.0
            return False
        return True

    def mute_remaining_seconds(
        self, chat_id: int, now: float | None = None,
    ) -> float:
        state = self._chats.get(chat_id)
        if state is None or state.mute_until == 0.0:
            return 0.0
        ts = now if now is not None else time.time()
        return max(0.0, state.mute_until - ts)
