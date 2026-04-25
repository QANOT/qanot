"""Per-user rate limiting — prevents spam and abuse.

OpenClaw-inspired: sliding window with lockout.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_WINDOW = 60  # seconds
DEFAULT_MAX_REQUESTS = 15  # per window
DEFAULT_LOCKOUT = 300  # 5 minutes


class RateLimiter:
    """Sliding window rate limiter per user.

    Tracks request timestamps per user_id. When a user exceeds
    max_requests within window_seconds, they are locked out
    for lockout_seconds.
    """

    def __init__(
        self,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        window_seconds: int = DEFAULT_WINDOW,
        lockout_seconds: int = DEFAULT_LOCKOUT,
    ):
        if max_requests < 1:
            raise ValueError(f"max_requests must be >= 1, got {max_requests}")
        if window_seconds < 1:
            raise ValueError(f"window_seconds must be >= 1, got {window_seconds}")
        if lockout_seconds < 0:
            raise ValueError(f"lockout_seconds must be >= 0, got {lockout_seconds}")
        self.max_requests = max_requests
        self.window = window_seconds
        self.lockout = lockout_seconds
        self._requests: dict[str, list[float]] = {}  # user_id → [timestamps]
        self._locked_until: dict[str, float] = {}  # user_id → unlock_time

    def check(self, user_id: str) -> tuple[bool, str]:
        """Check if user is allowed to make a request.

        Returns (allowed, reason). If not allowed, reason explains why.
        """
        now = time.monotonic()

        # Check lockout
        if (unlock_time := self._locked_until.get(user_id)) is not None:
            if now < unlock_time:
                return False, f"Rate limit: {int(unlock_time - now)}s qoldi"
            del self._locked_until[user_id]

        # Slide window: remove old timestamps
        cutoff = now - self.window
        timestamps = self._requests.setdefault(user_id, [])
        timestamps[:] = [t for t in timestamps if t > cutoff]

        # Check limit
        if len(timestamps) >= self.max_requests:
            self._locked_until[user_id] = now + self.lockout
            logger.warning(
                "Rate limit exceeded for user %s: %d requests in %ds → locked for %ds",
                user_id, len(timestamps), self.window, self.lockout,
            )
            return False, f"Juda ko'p so'rov. {self.lockout // 60} daqiqa kutib turing."

        return True, ""

    def record(self, user_id: str) -> None:
        """Record a successful request."""
        self._requests.setdefault(user_id, []).append(time.monotonic())

    def retry_after(self, user_id: str) -> int:
        """Seconds until the user can next make a request.

        Returns 0 if not currently rate-limited. Used by tools that want
        to surface a retry hint to the LLM in their error JSON.
        """
        now = time.monotonic()
        unlock_time = self._locked_until.get(user_id)
        if unlock_time is not None and now < unlock_time:
            return max(1, int(unlock_time - now))
        # Not locked: estimate based on oldest request in window.
        timestamps = self._requests.get(user_id, [])
        if len(timestamps) < self.max_requests:
            return 0
        oldest = min(timestamps)
        return max(1, int(oldest + self.window - now))

    def reset(self, user_id: str) -> None:
        """Reset rate limit for a user."""
        self._requests.pop(user_id, None)
        self._locked_until.pop(user_id, None)

    def cleanup(self) -> None:
        """Remove stale entries for users who haven't made requests recently."""
        now = time.monotonic()
        cutoff = now - self.window * 2
        self._requests = {
            uid: timestamps
            for uid, timestamps in self._requests.items()
            if timestamps and timestamps[-1] >= cutoff
        }
        self._locked_until = {
            uid: unlock_time
            for uid, unlock_time in self._locked_until.items()
            if now < unlock_time
        }
