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
        if user_id in self._locked_until:
            unlock_time = self._locked_until[user_id]
            if now < unlock_time:
                return False, f"Rate limit: {int(unlock_time - now)}s qoldi"
            del self._locked_until[user_id]

        # Slide window: remove old timestamps
        timestamps = self._requests.get(user_id, [])
        cutoff = now - self.window
        timestamps = [t for t in timestamps if t > cutoff]
        self._requests[user_id] = timestamps

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

    def reset(self, user_id: str) -> None:
        """Reset rate limit for a user."""
        self._requests.pop(user_id, None)
        self._locked_until.pop(user_id, None)

    def cleanup(self) -> None:
        """Remove stale entries for users who haven't made requests recently."""
        now = time.monotonic()
        cutoff = now - self.window * 2
        self._requests = {uid: ts for uid, ts in self._requests.items() if ts and ts[-1] >= cutoff}
        self._locked_until = {uid: t for uid, t in self._locked_until.items() if now < t}
