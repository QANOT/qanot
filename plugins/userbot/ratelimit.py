"""In-memory rate limiter for userbot sends.

Two independent buckets, both consulted on every send:

  1. Per-recipient cooldown — deny if the last send to this same recipient
     happened less than ``per_recipient_seconds`` ago. Protects a single
     contact from being flooded by the agent.

  2. Global hourly quota — deny if the count of sends in the last 3600s
     is ``>= hourly_global``. Caps blast radius across *all* recipients.

Per-process, in-memory only: restarts reset the state. That's fine —
Telegram's own flood protection is the hard backstop; these buckets exist
to keep the agent polite, not to be a bulletproof enforcement layer.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque


class RateLimitError(Exception):
    """Raised when a send would breach one of the buckets."""

    def __init__(self, bucket: str, retry_after_seconds: int, message: str) -> None:
        super().__init__(message)
        self.bucket = bucket
        self.retry_after_seconds = retry_after_seconds


@dataclass
class RateLimiter:
    per_recipient_seconds: int
    hourly_global: int

    def __post_init__(self) -> None:
        # Maps opaque recipient_id → last send timestamp (monotonic-ish; we
        # use wall-clock because the hourly bucket compares against wall-time
        # anyway and mixing the two leads to off-by-hour bugs.)
        self._last_send: dict[str, float] = {}
        # Sliding window of send timestamps within the last hour.
        self._hourly: Deque[float] = deque()

    # ── Public API ───────────────────────────────────────────────

    def check(self, recipient_id: str, *, now: float | None = None) -> None:
        """Raise :class:`RateLimitError` if a send right now would breach a bucket.

        Pure predicate — does NOT record anything. Call :meth:`record` after
        a *successful* send.
        """
        now = time.time() if now is None else now
        self._evict_hourly(now)

        # Per-recipient bucket first — it's cheaper and gives a more useful
        # "wait N seconds for this contact" error.
        last = self._last_send.get(recipient_id)
        if last is not None:
            elapsed = now - last
            if elapsed < self.per_recipient_seconds:
                retry = max(1, int(self.per_recipient_seconds - elapsed) + 1)
                raise RateLimitError(
                    bucket="per_recipient",
                    retry_after_seconds=retry,
                    message=(
                        f"Shu oluvchiga oxirgi xabar {int(elapsed)} soniya oldin yuborilgan. "
                        f"{retry} soniyadan keyin qayta urinib ko'ring."
                    ),
                )

        if len(self._hourly) >= self.hourly_global:
            # Oldest entry defines when the oldest slot expires.
            oldest = self._hourly[0]
            retry = max(1, int(3600 - (now - oldest)) + 1)
            raise RateLimitError(
                bucket="hourly_global",
                retry_after_seconds=retry,
                message=(
                    f"Soatlik limit tugadi ({self.hourly_global} xabar/soat). "
                    f"{retry} soniyadan keyin qayta urinib ko'ring."
                ),
            )

    def record(self, recipient_id: str, *, now: float | None = None) -> None:
        """Register a successful send."""
        now = time.time() if now is None else now
        self._last_send[recipient_id] = now
        self._hourly.append(now)
        self._evict_hourly(now)

    def _evict_hourly(self, now: float) -> None:
        # 3600s sliding window.
        cutoff = now - 3600.0
        while self._hourly and self._hourly[0] < cutoff:
            self._hourly.popleft()

    # ── Introspection (tests only) ───────────────────────────────

    def hourly_count(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        self._evict_hourly(now)
        return len(self._hourly)
