"""Shared error classification for providers and agent retry logic."""

from __future__ import annotations

# Error type constants — used across agent.py and failover.py
ERROR_RATE_LIMIT = "rate_limit"
ERROR_AUTH = "auth"
ERROR_BILLING = "billing"
ERROR_OVERLOADED = "overloaded"
ERROR_TIMEOUT = "timeout"
ERROR_NOT_FOUND = "not_found"
ERROR_UNKNOWN = "unknown"

# Permanent failures — don't retry or failover
PERMANENT_FAILURES = {ERROR_AUTH, ERROR_BILLING}
# Transient — retry or try next provider
TRANSIENT_FAILURES = {ERROR_RATE_LIMIT, ERROR_OVERLOADED, ERROR_TIMEOUT, ERROR_NOT_FOUND}

# HTTP status code mappings
_STATUS_MAP = {
    429: ERROR_RATE_LIMIT,
    401: ERROR_AUTH,
    403: ERROR_AUTH,
    402: ERROR_BILLING,
    404: ERROR_NOT_FOUND,
    503: ERROR_OVERLOADED,
    529: ERROR_OVERLOADED,
    408: ERROR_TIMEOUT,
    504: ERROR_TIMEOUT,
    500: ERROR_TIMEOUT,
    502: ERROR_TIMEOUT,
}


def classify_error(error: Exception) -> str:
    """Classify an API error by HTTP status code or message.

    Returns one of the ERROR_* constants above.
    """
    # Extract HTTP status code if available
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status and status in _STATUS_MAP:
        return _STATUS_MAP[status]

    # Fallback: check error message
    msg = str(error).lower()
    if "rate" in msg and "limit" in msg or "429" in msg:
        return ERROR_RATE_LIMIT
    if "overloaded" in msg or "503" in msg or "529" in msg:
        return ERROR_OVERLOADED
    if "unauthorized" in msg or "forbidden" in msg or "invalid" in msg and "key" in msg:
        return ERROR_AUTH
    if "billing" in msg or "402" in msg or "quota" in msg:
        return ERROR_BILLING
    if "timeout" in msg or "timed out" in msg:
        return ERROR_TIMEOUT
    if "not_found" in msg or "not found" in msg:
        return ERROR_NOT_FOUND
    return ERROR_UNKNOWN
