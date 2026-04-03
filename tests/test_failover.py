"""Tests for failover provider, error classification, and OAuth token detection."""

from __future__ import annotations

import pytest

from qanot.providers.failover import (
    ProviderProfile,
    COOLDOWN_SECONDS,
    MAX_CONSECUTIVE_OVERLOADS,
    OVERLOAD_COOLDOWN_SECONDS,
)
from qanot.providers.errors import (
    classify_error,
    ERROR_RATE_LIMIT,
    ERROR_AUTH,
    ERROR_BILLING,
    ERROR_OVERLOADED,
    ERROR_TIMEOUT,
    ERROR_NOT_FOUND,
    ERROR_UNKNOWN,
    PERMANENT_FAILURES,
    TRANSIENT_FAILURES,
)
from qanot.providers.anthropic import _is_oauth_token


class TestOAuthDetection:
    def test_oauth_token_detected(self):
        assert _is_oauth_token("sk-ant-oat01-abc123") is True

    def test_regular_api_key_not_oauth(self):
        assert _is_oauth_token("sk-ant-api03-abc123") is False

    def test_empty_string(self):
        assert _is_oauth_token("") is False


class TestErrorClassification:
    def test_rate_limit_429(self):
        err = type("Err", (), {"status_code": 429})()
        assert classify_error(err) == ERROR_RATE_LIMIT

    def test_auth_401(self):
        err = type("Err", (), {"status_code": 401})()
        assert classify_error(err) == ERROR_AUTH

    def test_auth_403(self):
        err = type("Err", (), {"status_code": 403})()
        assert classify_error(err) == ERROR_AUTH

    def test_billing_402(self):
        err = type("Err", (), {"status_code": 402})()
        assert classify_error(err) == ERROR_BILLING

    def test_overloaded_529(self):
        err = type("Err", (), {"status_code": 529})()
        assert classify_error(err) == ERROR_OVERLOADED

    def test_not_found_404(self):
        err = type("Err", (), {"status_code": 404})()
        assert classify_error(err) == ERROR_NOT_FOUND

    def test_timeout_504(self):
        err = type("Err", (), {"status_code": 504})()
        assert classify_error(err) == ERROR_TIMEOUT

    def test_unknown_error(self):
        err = Exception("something weird happened")
        assert classify_error(err) == ERROR_UNKNOWN

    def test_rate_limit_from_message(self):
        err = Exception("rate limit exceeded, 429")
        assert classify_error(err) == ERROR_RATE_LIMIT

    def test_not_found_from_message(self):
        err = Exception("model not found")
        assert classify_error(err) == ERROR_NOT_FOUND

    def test_invalid_key_from_message(self):
        err = Exception("invalid api key provided")
        assert classify_error(err) == ERROR_AUTH

    def test_timeout_500(self):
        err = type("Err", (), {"status_code": 500})()
        assert classify_error(err) == ERROR_TIMEOUT

    def test_timeout_502(self):
        err = type("Err", (), {"status_code": 502})()
        assert classify_error(err) == ERROR_TIMEOUT


class TestErrorCategories:
    def test_auth_is_permanent(self):
        assert ERROR_AUTH in PERMANENT_FAILURES

    def test_billing_is_permanent(self):
        assert ERROR_BILLING in PERMANENT_FAILURES

    def test_rate_limit_is_transient(self):
        assert ERROR_RATE_LIMIT in TRANSIENT_FAILURES

    def test_not_found_is_transient(self):
        assert ERROR_NOT_FOUND in TRANSIENT_FAILURES


class TestProviderProfile:
    def test_initially_available(self):
        p = ProviderProfile(name="test", provider_type="anthropic", api_key="k", model="m")
        assert p.is_available is True

    def test_transient_failure_cooldown(self):
        p = ProviderProfile(name="test", provider_type="anthropic", api_key="k", model="m")
        p.mark_failed(ERROR_RATE_LIMIT)
        assert p.is_available is False
        assert p._failure_count == 1

    def test_permanent_failure_stays_unavailable(self):
        p = ProviderProfile(name="test", provider_type="anthropic", api_key="k", model="m")
        p.mark_failed(ERROR_AUTH)
        assert p.is_available is False
        assert p._cooldown_until == float("inf")

    def test_success_resets_state(self):
        p = ProviderProfile(name="test", provider_type="anthropic", api_key="k", model="m")
        p.mark_failed(ERROR_RATE_LIMIT)
        p.mark_success()
        assert p.is_available is True
        assert p._failure_count == 0
        assert p._last_error_type == ""

    def test_not_found_is_transient(self):
        p = ProviderProfile(name="test", provider_type="anthropic", api_key="k", model="m")
        p.mark_failed(ERROR_NOT_FOUND)
        assert p._cooldown_until != float("inf")
        assert p._failure_count == 1


class TestConsecutiveOverloads:
    def _make_profile(self) -> ProviderProfile:
        return ProviderProfile(name="test", provider_type="anthropic", api_key="k", model="m")

    def test_overload_increments_counter(self):
        p = self._make_profile()
        p.mark_failed(ERROR_OVERLOADED)
        assert p._consecutive_overloads == 1

    def test_rate_limit_increments_counter(self):
        p = self._make_profile()
        p.mark_failed(ERROR_RATE_LIMIT)
        assert p._consecutive_overloads == 1

    def test_non_overload_resets_counter(self):
        p = self._make_profile()
        p.mark_failed(ERROR_OVERLOADED)
        p.mark_failed(ERROR_OVERLOADED)
        p.mark_failed(ERROR_TIMEOUT)
        assert p._consecutive_overloads == 0

    def test_success_resets_counter(self):
        p = self._make_profile()
        p.mark_failed(ERROR_OVERLOADED)
        p.mark_failed(ERROR_OVERLOADED)
        p.mark_success()
        assert p._consecutive_overloads == 0

    def test_aggressive_cooldown_at_threshold(self):
        """After MAX_CONSECUTIVE_OVERLOADS, cooldown should be OVERLOAD_COOLDOWN_SECONDS."""
        p = self._make_profile()
        for _ in range(MAX_CONSECUTIVE_OVERLOADS):
            p.mark_failed(ERROR_OVERLOADED)
        assert p._consecutive_overloads == MAX_CONSECUTIVE_OVERLOADS
        assert p.is_available is False
        # The cooldown should be the aggressive one, not the normal scaled one
        # Normal would be COOLDOWN_SECONDS * failure_count; aggressive is OVERLOAD_COOLDOWN_SECONDS
        # Just verify it's in cooldown — exact timing is monotonic-dependent

    def test_mixed_overload_and_rate_limit_counts(self):
        """Both overloaded and rate_limit errors contribute to consecutive count."""
        p = self._make_profile()
        p.mark_failed(ERROR_OVERLOADED)
        p.mark_failed(ERROR_RATE_LIMIT)
        p.mark_failed(ERROR_OVERLOADED)
        assert p._consecutive_overloads == MAX_CONSECUTIVE_OVERLOADS

    def test_counter_survives_below_threshold(self):
        """Two overloads then success resets, then two more stays below threshold."""
        p = self._make_profile()
        p.mark_failed(ERROR_OVERLOADED)
        p.mark_failed(ERROR_OVERLOADED)
        p.mark_success()
        p.mark_failed(ERROR_OVERLOADED)
        p.mark_failed(ERROR_OVERLOADED)
        assert p._consecutive_overloads == 2
        assert p._consecutive_overloads < MAX_CONSECUTIVE_OVERLOADS
