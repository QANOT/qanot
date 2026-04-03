"""Tests for Anthropic provider context overflow retry logic."""

from __future__ import annotations

import pytest

from qanot.providers.anthropic import _parse_overflow, _MIN_MAX_TOKENS


# ── _parse_overflow tests ───────────────────────────────────


class TestParseOverflow:
    """Unit tests for the overflow error parser."""

    def test_standard_overflow_message(self):
        msg = "input length and `max_tokens` exceed context limit: 190000 + 8192 > 200000"
        result = _parse_overflow(msg)
        assert result == (190000, 8192, 200000)

    def test_tight_spacing(self):
        msg = "exceed context limit: 195000+4096>200000"
        result = _parse_overflow(msg)
        assert result == (195000, 4096, 200000)

    def test_extra_whitespace(self):
        msg = "190000  +  8192  >  200000"
        result = _parse_overflow(msg)
        assert result == (190000, 8192, 200000)

    def test_non_overflow_error(self):
        msg = "rate_limit_error: too many requests"
        assert _parse_overflow(msg) is None

    def test_empty_string(self):
        assert _parse_overflow("") is None

    def test_partial_match_no_gt(self):
        msg = "190000 + 8192"
        assert _parse_overflow(msg) is None

    def test_large_context_window(self):
        msg = "exceed context limit: 950000 + 8192 > 1000000"
        result = _parse_overflow(msg)
        assert result == (950000, 8192, 1000000)


class TestOverflowMaxTokensCalculation:
    """Verify the max_tokens floor and calculation logic."""

    def test_new_max_respects_floor(self):
        """When context is nearly full, max_tokens should not go below _MIN_MAX_TOKENS."""
        input_tokens = 199500
        context_limit = 200000
        safety_buffer = 1000
        new_max = max(_MIN_MAX_TOKENS, context_limit - input_tokens - safety_buffer)
        assert new_max == _MIN_MAX_TOKENS

    def test_new_max_normal_case(self):
        """Normal overflow: enough room for a reduced max_tokens."""
        input_tokens = 190000
        context_limit = 200000
        safety_buffer = 1000
        new_max = max(_MIN_MAX_TOKENS, context_limit - input_tokens - safety_buffer)
        assert new_max == 9000

    def test_floor_constant_value(self):
        assert _MIN_MAX_TOKENS == 1024
