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
    if len(text) <= max_chars:
        return text
    head = int(max_chars * head_ratio)
    tail = int(max_chars * tail_ratio)
    removed = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n... [truncated {removed} chars] ...\n\n"
        + text[-tail:]
    )
