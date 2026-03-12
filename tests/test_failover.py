"""Tests for failover provider and OAuth token detection."""

from __future__ import annotations

import time
import pytest

from qanot.providers.failover import (
    ProviderProfile,
    _classify_error,
    COOLDOWN_SECONDS,
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
        assert _classify_error(err) == "rate_limit"

    def test_auth_401(self):
        err = type("Err", (), {"status_code": 401})()
        assert _classify_error(err) == "auth"

    def test_auth_403(self):
        err = type("Err", (), {"status_code": 403})()
        assert _classify_error(err) == "auth"

    def test_billing_402(self):
        err = type("Err", (), {"status_code": 402})()
        assert _classify_error(err) == "billing"

    def test_overloaded_529(self):
        err = type("Err", (), {"status_code": 529})()
        assert _classify_error(err) == "overloaded"

    def test_not_found_404(self):
        err = type("Err", (), {"status_code": 404})()
        assert _classify_error(err) == "not_found"

    def test_timeout_504(self):
        err = type("Err", (), {"status_code": 504})()
        assert _classify_error(err) == "timeout"

    def test_unknown_error(self):
        err = Exception("something weird happened")
        assert _classify_error(err) == "unknown"

    def test_rate_limit_from_message(self):
        err = Exception("rate limit exceeded, 429")
        assert _classify_error(err) == "rate_limit"

    def test_not_found_from_message(self):
        err = Exception("model not found")
        assert _classify_error(err) == "not_found"


class TestProviderProfile:
    def test_initially_available(self):
        p = ProviderProfile(name="test", provider_type="anthropic", api_key="k", model="m")
        assert p.is_available is True

    def test_transient_failure_cooldown(self):
        p = ProviderProfile(name="test", provider_type="anthropic", api_key="k", model="m")
        p.mark_failed("rate_limit")
        assert p.is_available is False
        assert p._failure_count == 1

    def test_permanent_failure_stays_unavailable(self):
        p = ProviderProfile(name="test", provider_type="anthropic", api_key="k", model="m")
        p.mark_failed("auth")
        assert p.is_available is False
        assert p._cooldown_until == float("inf")

    def test_success_resets_state(self):
        p = ProviderProfile(name="test", provider_type="anthropic", api_key="k", model="m")
        p.mark_failed("rate_limit")
        p.mark_success()
        assert p.is_available is True
        assert p._failure_count == 0
        assert p._last_error_type == ""

    def test_not_found_is_transient(self):
        p = ProviderProfile(name="test", provider_type="anthropic", api_key="k", model="m")
        p.mark_failed("not_found")
        # Should be in cooldown, not permanent
        assert p._cooldown_until != float("inf")
        assert p._failure_count == 1
