"""Shared utilities for Qanot AI."""

from __future__ import annotations


_TRUNCATION_MARKER = "\n\n... [truncated {} chars] ...\n\n"


def truncate_with_marker(
    text: str,
    max_chars: int,
    head_ratio: float = 0.70,
    tail_ratio: float = 0.20,
) -> str:
    """Truncate text keeping head and tail with a marker in the middle.

    Default: keeps first 70% and last 20%, with a gap marker.
    """
    text_len = len(text)
    if text_len <= max_chars:
        return text
    # Upper-bound marker length: removed <= text_len so digit count never exceeds this.
    marker_overhead = len(_TRUNCATION_MARKER.format(text_len))
    budget = max(max_chars - marker_overhead, 0)
    head = int(budget * head_ratio)
    tail = int(budget * tail_ratio)
    removed = text_len - head - tail
    if removed <= 0:
        # Ratios sum to >= 1.0 for this max_chars; just hard-truncate
        return text[:max_chars]
    tail_text = text[-tail:] if tail else ""
    return text[:head] + _TRUNCATION_MARKER.format(removed) + tail_text
