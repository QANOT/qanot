"""Diagnostic tools — operator-facing introspection.

Tools: compaction_stats, cache_stats. Future: memory_stats, eval_history.
"""

from __future__ import annotations

import json
from pathlib import Path

from qanot.cache_stats import (
    aggregate_session_files,
    format_health_assessment,
)
from qanot.compaction_metrics import load_events, summarize_events
from qanot.registry import ToolRegistry


def register_diagnostics_tools(registry: ToolRegistry, workspace_dir: str) -> None:
    async def compaction_stats(params: dict) -> str:
        try:
            days = int(params.get("days") or 7)
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(days, 90))
        try:
            limit = int(params.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 500))
        events = load_events(workspace_dir, days=days, limit=limit)
        summary = summarize_events(events)
        return json.dumps({
            "window_days": days,
            "summary": summary,
            "recent_events": [
                {
                    "ts": e.get("ts", "")[:19],  # YYYY-MM-DDTHH:MM:SS
                    "stage": e.get("stage"),
                    "tokens_before": e.get("tokens_before"),
                    "tokens_after": e.get("tokens_after"),
                    "messages_before": e.get("messages_before"),
                    "duration_ms": e.get("duration_ms"),
                    "error": e.get("error", ""),
                }
                for e in events[:10]
            ],
            "note": (
                "Compaction events log every multi-stage summarization. "
                "High error_rate or merge_failures suggest tuning chunk_ratio or parts. "
                "Foundation for Tier 4 auto-tuning (not yet implemented)."
            ),
        }, ensure_ascii=False)

    registry.register(
        name="compaction_stats",
        description=(
            "Show compaction event statistics: error rate, average compression, stage "
            "distribution, recent events. `days` (1-90, default 7) is the lookback window. "
            "`limit` caps the number of recent events shown (default 50, max 500). "
            "Operator-facing diagnostic — use to spot compaction regressions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "days": {"type": "number", "description": "Lookback window in days (default 7, max 90)"},
                "limit": {"type": "number", "description": "Max events to return (default 50, max 500)"},
            },
        },
        handler=compaction_stats,
    )

    async def cache_stats(params: dict) -> str:
        try:
            days = int(params.get("days") or 7)
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(days, 90))

        # Sessions live at <workspace>/sessions/. Standard layout per
        # qanot/session.py.
        sessions_dir = Path(workspace_dir) / "sessions"
        summary = aggregate_session_files(sessions_dir, days=days)
        return json.dumps({
            "window_days": days,
            "summary": summary.to_dict(),
            "health": format_health_assessment(summary),
            "interpretation": {
                "cache_read_ratio": (
                    "cache_read_tokens / (cache_read + input + cache_write). "
                    "≥0.85 = excellent, ≥0.70 = healthy, <0.50 = cache thrash."
                ),
                "avg_input_per_turn": (
                    "Uncached prefix tokens billed per turn. Should be small "
                    "(only the dynamic suffix). Climbing = stable prefix is "
                    "mutating mid-conversation."
                ),
            },
            "note": (
                "Aggregated from session JSONL files. Lower cache_read_ratio "
                "suggests the stable prefix is changing more than expected — "
                "audit recent SOUL.md/USER.md/TOOLS.md edits or plugin reloads."
            ),
        }, ensure_ascii=False)

    registry.register(
        name="cache_stats",
        description=(
            "Aggregate Anthropic prompt-cache hit rate across recent sessions. "
            "Reports cache_read_ratio (1.0 = perfect, ~0.85+ = healthy), avg "
            "tokens per turn, and total cost. `days` (1-90, default 7) is the "
            "lookback window. Operator-facing diagnostic — use to spot prefix "
            "mutations that are tanking the cache."
        ),
        parameters={
            "type": "object",
            "properties": {
                "days": {
                    "type": "number",
                    "description": "Lookback window in days (default 7, max 90).",
                },
            },
        },
        handler=cache_stats,
    )
