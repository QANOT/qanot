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


def register_diagnostics_tools(
    registry: ToolRegistry,
    workspace_dir: str,
    sessions_dir: str = "",
) -> None:
    # Sessions live OUTSIDE the workspace by default (Config default:
    # /data/sessions). Caller passes the config value so cache_stats
    # finds the right directory.
    if not sessions_dir:
        sessions_dir = "/data/sessions"
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

        summary = aggregate_session_files(Path(sessions_dir), days=days)
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

    async def cost_stats(params: dict) -> str:
        """Aggregate per-user cost across the persistent CostTracker ledger
        (workspace/costs.json). Reports total + daily spend per user,
        plus top spenders for the lookback window."""
        try:
            top_n = int(params.get("top_n") or 10)
        except (TypeError, ValueError):
            top_n = 10
        top_n = max(1, min(top_n, 100))
        user_filter = (params.get("user_id") or "").strip()

        from qanot.cost import CostTracker
        tracker = CostTracker(workspace_dir)
        all_stats = tracker.get_all_stats()

        if user_filter:
            entry = all_stats.get(user_filter)
            if entry is None:
                return json.dumps({"error": f"no cost record for user_id {user_filter!r}"})
            return json.dumps({
                "user_id": user_filter,
                "stats": entry,
            }, ensure_ascii=False)

        # Sort by total_cost descending
        ranked = sorted(
            all_stats.items(),
            key=lambda kv: float(kv[1].get("total_cost", 0.0) or 0.0),
            reverse=True,
        )[:top_n]

        return json.dumps({
            "total_users_tracked": len(all_stats),
            "total_cost_usd_all_users": round(tracker.get_total_cost(), 4),
            "top_spenders": [
                {
                    "user_id": uid,
                    "total_cost_usd": round(float(s.get("total_cost", 0.0) or 0.0), 4),
                    "daily_cost_usd": round(float(s.get("daily_cost", 0.0) or 0.0), 4),
                    "daily_date": s.get("daily_date", ""),
                    "turns": int(s.get("turns", 0) or 0),
                    "api_calls": int(s.get("api_calls", 0) or 0),
                    "input_tokens": int(s.get("input_tokens", 0) or 0),
                    "output_tokens": int(s.get("output_tokens", 0) or 0),
                    "cache_read_tokens": int(s.get("cache_read_tokens", 0) or 0),
                }
                for uid, s in ranked
            ],
            "note": (
                "Aggregated from <workspace>/costs.json (persistent per-user "
                "ledger). Pass user_id to inspect one user; top_n caps the "
                "spender list (default 10, max 100)."
            ),
        }, ensure_ascii=False)

    registry.register(
        name="cost_stats",
        description=(
            "Per-user cost ledger summary: top spenders, daily/total cost, "
            "tokens by category. Use to spot users who are burning budget or "
            "to verify that per-turn caps caught a runaway loop. "
            "`user_id` (optional): inspect one user's full record. "
            "`top_n` (default 10, max 100): how many top-spending users to "
            "list. Operator-facing diagnostic."
        ),
        parameters={
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Inspect one user's full record (optional).",
                },
                "top_n": {
                    "type": "number",
                    "description": "Top spenders to list (default 10, max 100).",
                },
            },
        },
        handler=cost_stats,
    )
