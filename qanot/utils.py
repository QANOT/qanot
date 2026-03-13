"""Shared utilities for Qanot AI."""

from __future__ import annotations


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
    # Estimate marker overhead so total output stays within max_chars.
    # Marker template: "\n\n... [truncated NNNNN chars] ...\n\n"
    # Estimate with worst-case digit count for removed chars.
    marker_overhead = len("\n\n... [truncated  chars] ...\n\n") + len(str(text_len))
    budget = max(max_chars - marker_overhead, 0)
    head = int(budget * head_ratio)
    tail = int(budget * tail_ratio)
    removed = text_len - head - tail
    if removed <= 0:
        # Ratios sum to >= 1.0 for this max_chars; just hard-truncate
        return text[:max_chars]
    marker = f"\n\n... [truncated {removed} chars] ...\n\n"
    if tail == 0:
        return text[:head] + marker
    return text[:head] + marker + text[-tail:]
