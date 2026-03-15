"""Shared error classification for providers and agent retry logic."""

from __future__ import annotations

# Error type constants — used across agent.py and failover.py
ERROR_RATE_LIMIT = "rate_limit"
ERROR_AUTH = "auth"
ERROR_BILLING = "billing"
ERROR_OVERLOADED = "overloaded"
ERROR_TIMEOUT = "timeout"
ERROR_NOT_FOUND = "not_found"
ERROR_CONTEXT_OVERFLOW = "context_overflow"
ERROR_UNKNOWN = "unknown"

# Permanent failures — don't retry or failover
PERMANENT_FAILURES = {ERROR_AUTH, ERROR_BILLING}
# Transient — retry or try next provider
TRANSIENT_FAILURES = {ERROR_RATE_LIMIT, ERROR_OVERLOADED, ERROR_TIMEOUT, ERROR_NOT_FOUND}
# Recoverable via compaction
COMPACTION_FAILURES = {ERROR_CONTEXT_OVERFLOW}

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
    # Detect Python built-in timeout/connection errors early
    if isinstance(error, (TimeoutError, ConnectionAbortedError)):
        return ERROR_TIMEOUT
    if isinstance(error, ConnectionRefusedError):
        return ERROR_OVERLOADED

    # Extract HTTP status code if available
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status is not None:
        try:
            status = int(status)
        except (ValueError, TypeError):
            status = None
    if status is not None:
        mapped = _STATUS_MAP.get(status)
        if mapped is not None:
            return mapped

    # Fallback: check error message
    msg = str(error).lower()

    # Context overflow detection (before rate limit — some providers use 400)
    if is_context_overflow_error(msg):
        return ERROR_CONTEXT_OVERFLOW

    if ("rate" in msg and "limit" in msg) or "429" in msg:
        return ERROR_RATE_LIMIT
    if "overloaded" in msg or "503" in msg or "529" in msg:
        return ERROR_OVERLOADED
    if "unauthorized" in msg or "forbidden" in msg or ("invalid" in msg and "key" in msg):
        return ERROR_AUTH
    if "billing" in msg or "402" in msg or "quota" in msg:
        return ERROR_BILLING
    if "timeout" in msg or "timed out" in msg:
        return ERROR_TIMEOUT
    if "not_found" in msg or "not found" in msg:
        return ERROR_NOT_FOUND
    return ERROR_UNKNOWN


# Context overflow patterns from all major providers
_OVERFLOW_PATTERNS = [
    "context_window_exceeded",
    "context length exceeded",
    "maximum context length",
    "request_too_large",
    "request exceeds the maximum size",
    "prompt is too long",
    "exceeds model context window",
    "too many tokens",
    "max_tokens",
    "token limit",
    "content would exceed",
]


def is_context_overflow_error(msg: str) -> bool:
    """Check if an error message indicates context window overflow."""
    lower = msg.lower()
    return any(pattern in lower for pattern in _OVERFLOW_PATTERNS)
