"""Circuit breaker — loop detection, fingerprinting, deterministic error checks."""

from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

MAX_SAME_ACTION = 3  # Break after N identical consecutive tool calls

# Errors that should NOT be retried (deterministic failures)
DETERMINISTIC_ERRORS = (
    "unknown tool",
    "missing required",
    "invalid parameter",
    "not found",
    "permission denied",
    "validation error",
    "invalid input",
)

_VERBOSE_KEYS = frozenset({"details", "debug", "trace", "raw", "stacktrace", "verbose", "raw_response"})


def tool_call_fingerprint(name: str, input_data: dict) -> str:
    """Hash a tool call for duplicate detection."""
    raw = f"{name}:{json.dumps(input_data, sort_keys=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def result_fingerprint(result: str) -> str:
    """Hash a tool result for no-progress detection."""
    return hashlib.sha256(result.encode()).hexdigest()[:16]


def strip_verbose_result(result: str) -> str:
    """Strip verbose fields from tool results to save context tokens.

    Removes common bloat fields like 'details', 'debug', 'trace', 'raw'
    from JSON results while preserving the core data.
    """
    if not result or result[0] != '{':
        return result
    try:
        data = json.loads(result)
        if not isinstance(data, dict):
            return result
        stripped = False
        for key in _VERBOSE_KEYS:
            if key in data:
                val = data[key]
                if isinstance(val, str) and len(val) > 200:
                    data[key] = val[:100] + f"... [{len(val)} chars stripped]"
                    stripped = True
                elif isinstance(val, (list, dict)) and len(json.dumps(val)) > 500:
                    data[key] = f"[{type(val).__name__} with {len(val)} items stripped]"
                    stripped = True
        return json.dumps(data) if stripped else result
    except (json.JSONDecodeError, TypeError):
        return result


def is_deterministic_error(result: str) -> bool:
    """Check if a tool error is deterministic (should not be retried)."""
    try:
        data = json.loads(result)
        error = data.get("error", "").lower()
        return any(marker in error for marker in DETERMINISTIC_ERRORS)
    except (json.JSONDecodeError, AttributeError):
        return False


def is_loop_detected(recent_fingerprints: list[str], new_key: str) -> bool:
    """Check if adding new_key would create a loop BEFORE executing tools.

    Detects:
    1. Same exact call repeated N times
    2. Alternating patterns (A-B-A-B)
    """
    # Check exact repetition
    recent_same = sum(1 for fp in recent_fingerprints if fp == new_key)
    if recent_same >= MAX_SAME_ACTION - 1:  # Would be Nth occurrence
        return True

    # Check alternating pattern (A-B-A-B) in last 4
    if len(recent_fingerprints) >= 3:
        last4 = recent_fingerprints[-3:] + [new_key]
        if len(last4) == 4 and last4[0] == last4[2] and last4[1] == last4[3] and last4[0] != last4[1]:
            return True

    return False


def is_no_progress(result_history: list[tuple[str, str]], call_key: str, result_hash: str) -> bool:
    """Detect no-progress: same call producing same result repeatedly.

    Args:
        result_history: list of (call_fingerprint, result_hash) tuples.

    Returns True if the same call+result pair has occurred 2+ times already.
    """
    pair = (call_key, result_hash)
    same_count = sum(1 for entry in result_history if entry == pair)
    return same_count >= 2
