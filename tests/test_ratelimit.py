"""Tests for ratelimit — per-user sliding window rate limiter."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from qanot.ratelimit import RateLimiter, DEFAULT_MAX_REQUESTS, DEFAULT_WINDOW, DEFAULT_LOCKOUT


# ── RateLimiter.check / record ───────────────────────────────


class TestRateLimiterBasic:
    """Test basic allow/block behavior."""

    def test_allows_under_limit(self) -> None:
        rl = RateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            allowed, reason = rl.check("user1")
            assert allowed is True
            assert reason == ""
            rl.record("user1")

    def test_blocks_over_limit(self) -> None:
        rl = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            rl.record("user1")
        allowed, reason = rl.check("user1")
        assert allowed is False
        assert reason  # non-empty reason

    def test_exactly_at_limit(self) -> None:
        """At exactly max_requests, next check should block."""
        rl = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            rl.record("user1")
        allowed, _ = rl.check("user1")
        assert allowed is False

    def test_lockout_message_in_uzbek(self) -> None:
        rl = RateLimiter(max_requests=2, window_seconds=60, lockout_seconds=300)
        rl.record("user1")
        rl.record("user1")
        _, reason = rl.check("user1")
        assert "daqiqa" in reason or "kutib" in reason


# ── Window expiry ────────────────────────────────────────────


class TestRateLimiterWindow:
    """Test sliding window expiry and lockout recovery."""

    def test_window_expiry_allows_again(self) -> None:
        """After window passes, old requests drop off and user is allowed."""
        rl = RateLimiter(max_requests=2, window_seconds=60, lockout_seconds=10)

        # Use a controllable monotonic clock
        fake_time = [1000.0]

        def mock_monotonic():
            return fake_time[0]

        with patch("qanot.ratelimit.time.monotonic", side_effect=mock_monotonic):
            rl.record("user1")
            rl.record("user1")
            allowed, _ = rl.check("user1")
            assert allowed is False  # locked out

        # Advance past lockout
        fake_time[0] = 1000.0 + 11.0
        with patch("qanot.ratelimit.time.monotonic", return_value=fake_time[0]):
            # Lockout expired
            allowed, _ = rl.check("user1")
            # Still may be blocked by window — advance past window too

        fake_time[0] = 1000.0 + 61.0
        with patch("qanot.ratelimit.time.monotonic", return_value=fake_time[0]):
            allowed, _ = rl.check("user1")
            assert allowed is True

    def test_lockout_blocks_during_period(self) -> None:
        rl = RateLimiter(max_requests=2, window_seconds=60, lockout_seconds=300)

        fake_time = [1000.0]

        with patch("qanot.ratelimit.time.monotonic", return_value=fake_time[0]):
            rl.record("user1")
            rl.record("user1")
            allowed, _ = rl.check("user1")
            assert allowed is False

        # Still within lockout (150s later, lockout is 300s)
        fake_time[0] = 1150.0
        with patch("qanot.ratelimit.time.monotonic", return_value=fake_time[0]):
            allowed, reason = rl.check("user1")
            assert allowed is False
            assert "qoldi" in reason


# ── Multi-user isolation ─────────────────────────────────────


class TestRateLimiterIsolation:
    """Test that rate limiting is per-user."""

    def test_users_isolated(self) -> None:
        rl = RateLimiter(max_requests=2, window_seconds=60)
        # Max out user1
        rl.record("user1")
        rl.record("user1")
        allowed_u1, _ = rl.check("user1")
        assert allowed_u1 is False

        # user2 should still be fine
        allowed_u2, _ = rl.check("user2")
        assert allowed_u2 is True

    def test_many_users(self) -> None:
        rl = RateLimiter(max_requests=5, window_seconds=60)
        for i in range(10):
            uid = f"user_{i}"
            rl.record(uid)
            allowed, _ = rl.check(uid)
            assert allowed is True


# ── Burst handling ───────────────────────────────────────────


class TestRateLimiterBurst:
    def test_rapid_burst_blocked(self) -> None:
        """All requests at same timestamp should trigger lockout."""
        rl = RateLimiter(max_requests=3, window_seconds=60)
        fake_time = 1000.0
        with patch("qanot.ratelimit.time.monotonic", return_value=fake_time):
            for _ in range(3):
                rl.record("burst_user")
            allowed, _ = rl.check("burst_user")
            assert allowed is False


# ── Cleanup ──────────────────────────────────────────────────


class TestRateLimiterCleanup:
    def test_cleanup_removes_stale(self) -> None:
        rl = RateLimiter(max_requests=5, window_seconds=60)

        fake_time = [1000.0]
        with patch("qanot.ratelimit.time.monotonic", return_value=fake_time[0]):
            rl.record("stale_user")

        # Advance past 2x window
        fake_time[0] = 1000.0 + 121.0
        with patch("qanot.ratelimit.time.monotonic", return_value=fake_time[0]):
            rl.cleanup()
        assert "stale_user" not in rl._requests

    def test_cleanup_keeps_active(self) -> None:
        rl = RateLimiter(max_requests=5, window_seconds=60)

        fake_time = [1000.0]
        with patch("qanot.ratelimit.time.monotonic", return_value=fake_time[0]):
            rl.record("active_user")

        # Within 2x window
        fake_time[0] = 1000.0 + 100.0
        with patch("qanot.ratelimit.time.monotonic", return_value=fake_time[0]):
            rl.cleanup()
        assert "active_user" in rl._requests

    def test_cleanup_removes_expired_lockout(self) -> None:
        rl = RateLimiter(max_requests=2, window_seconds=60, lockout_seconds=30)

        fake_time = [1000.0]
        with patch("qanot.ratelimit.time.monotonic", return_value=fake_time[0]):
            rl.record("locked_user")
            rl.record("locked_user")
            rl.check("locked_user")  # triggers lockout

        assert "locked_user" in rl._locked_until

        # Advance past lockout
        fake_time[0] = 1031.0
        with patch("qanot.ratelimit.time.monotonic", return_value=fake_time[0]):
            rl.cleanup()
        assert "locked_user" not in rl._locked_until


# ── Reset ────────────────────────────────────────────────────


class TestRateLimiterReset:
    def test_reset_clears_user(self) -> None:
        rl = RateLimiter(max_requests=2, window_seconds=60)
        rl.record("user1")
        rl.record("user1")
        rl.check("user1")  # triggers lockout
        rl.reset("user1")
        allowed, _ = rl.check("user1")
        assert allowed is True

    def test_reset_nonexistent_user_no_error(self) -> None:
        rl = RateLimiter()
        rl.reset("ghost_user")  # should not raise
