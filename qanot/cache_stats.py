"""Cache hit rate aggregation across session JSONL files.

Anthropic prompt caching writes new prefixes (cache_creation_input_tokens,
expensive) and reads cached prefixes (cache_read_input_tokens, ~10× cheaper
than uncached input). Healthy caching = high read / (read + write + input)
ratio.

The session logger already records per-turn usage data with these
fields. This module aggregates across recent sessions so an operator
can answer:
  - "What's our cache hit rate?"
  - "Are we paying for cache writes more than expected?"
  - "Did the recent prompt change tank the cache?"

Foundation for Hermes-borrow item #4 (cache-aware budget discipline):
without measurement you can't tune the budget.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CacheUsageSummary:
    turns: int
    total_input_tokens: int          # uncached new prefix portion
    total_cache_read_tokens: int     # served from cache (cheap)
    total_cache_write_tokens: int    # written to cache (expensive)
    total_output_tokens: int
    total_cost_usd: float

    @property
    def cache_read_ratio(self) -> float:
        """cache_read / (cache_read + input + cache_write). 1.0 = perfect."""
        denom = (self.total_cache_read_tokens
                 + self.total_input_tokens
                 + self.total_cache_write_tokens)
        return round(self.total_cache_read_tokens / denom, 4) if denom else 0.0

    @property
    def avg_input_per_turn(self) -> int:
        return self.total_input_tokens // self.turns if self.turns else 0

    @property
    def avg_cache_read_per_turn(self) -> int:
        return self.total_cache_read_tokens // self.turns if self.turns else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "turns": self.turns,
            "input_tokens": self.total_input_tokens,
            "cache_read_tokens": self.total_cache_read_tokens,
            "cache_write_tokens": self.total_cache_write_tokens,
            "output_tokens": self.total_output_tokens,
            "cost_usd": round(self.total_cost_usd, 4),
            "cache_read_ratio": self.cache_read_ratio,
            "avg_input_per_turn": self.avg_input_per_turn,
            "avg_cache_read_per_turn": self.avg_cache_read_per_turn,
        }


def aggregate_session_files(
    sessions_dir: str | Path,
    *,
    days: int = 7,
) -> CacheUsageSummary:
    """Walk recent session JSONL files and aggregate cache usage.

    Skips files older than `days`; tolerates corrupt JSONL lines.
    """
    sessions_path = Path(sessions_dir)
    if not sessions_path.exists():
        return CacheUsageSummary(0, 0, 0, 0, 0, 0.0)

    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    turns = 0
    sums = {"input": 0, "cacheRead": 0, "cacheWrite": 0, "output": 0}
    cost_usd = 0.0

    for path in sessions_path.glob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_ts:
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            usage = obj.get("usage")
            if not isinstance(usage, dict):
                continue
            turns += 1
            sums["input"] += int(usage.get("input", 0) or 0)
            sums["cacheRead"] += int(usage.get("cacheRead", 0) or 0)
            sums["cacheWrite"] += int(usage.get("cacheWrite", 0) or 0)
            sums["output"] += int(usage.get("output", 0) or 0)
            cost = usage.get("cost")
            if isinstance(cost, dict):
                try:
                    cost_usd += float(cost.get("total", 0) or 0)
                except (TypeError, ValueError):
                    pass

    return CacheUsageSummary(
        turns=turns,
        total_input_tokens=sums["input"],
        total_cache_read_tokens=sums["cacheRead"],
        total_cache_write_tokens=sums["cacheWrite"],
        total_output_tokens=sums["output"],
        total_cost_usd=cost_usd,
    )


def format_health_assessment(summary: CacheUsageSummary) -> str:
    """One-line human-readable assessment of caching health."""
    if summary.turns == 0:
        return "no recent turns recorded"
    ratio = summary.cache_read_ratio
    if ratio >= 0.85:
        return f"excellent ({ratio:.1%} cache reads)"
    if ratio >= 0.70:
        return f"healthy ({ratio:.1%} cache reads)"
    if ratio >= 0.50:
        return f"degraded ({ratio:.1%} cache reads — investigate prefix mutations)"
    return f"poor ({ratio:.1%} cache reads — likely cache thrash; audit prompt builder)"
