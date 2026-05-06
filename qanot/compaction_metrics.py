"""Append-only event log for compaction events.

Each time the agent compacts its conversation, we log a metric record
to <workspace>/memory/compaction_events.jsonl. This is the substrate
for future auto-tuning (Tier 4 in the 2026 roadmap):

  - Was the compaction successful? (Did the next turn succeed?)
  - How much did we compress? (tokens before/after)
  - Which stage did we end at? (full / partial / metadata-only / fallback)
  - How many turns of follow-up before compaction-recovery was needed?

For v1 we just LOG. Auto-tuning ("if metadata-only fallback rate > 10%,
adjust chunk_ratio") comes when we have enough data to learn from.

Schema (one JSON object per line):
{
  "ts": ISO8601,
  "user_id": str (optional),
  "tokens_before": int,
  "tokens_after": int,
  "messages_before": int,
  "messages_after": int,
  "stage": "full" | "partial" | "metadata-only" | "skipped",
  "parts_attempted": int,
  "parts_succeeded": int,
  "merge_succeeded": bool,
  "duration_ms": int,
  "error": str (only on failure)
}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EVENTS_FILENAME = "memory/compaction_events.jsonl"
MAX_EVENTS_ON_DISK = 5000


def log_compaction_event(
    workspace_dir: str,
    *,
    tokens_before: int = 0,
    tokens_after: int = 0,
    messages_before: int = 0,
    messages_after: int = 0,
    stage: str = "full",
    parts_attempted: int = 1,
    parts_succeeded: int = 1,
    merge_succeeded: bool = True,
    duration_ms: int = 0,
    user_id: str = "",
    error: str = "",
    tokens_before_prune: int = 0,
    bytes_pruned: int = 0,
) -> None:
    """Append one compaction event. Best-effort — never raises.

    Called from the compaction loop after each compact attempt. The
    metrics writer is designed to never crash the agent's main path:
    any IO/serialization failure logs a warning and returns silently.
    """
    try:
        if stage not in ("full", "partial", "metadata-only", "skipped", "error"):
            stage = "error"
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "user_id": (user_id or "").strip()[:64],
            "tokens_before": int(tokens_before),
            "tokens_after": int(tokens_after),
            "messages_before": int(messages_before),
            "messages_after": int(messages_after),
            "stage": stage,
            "parts_attempted": int(parts_attempted),
            "parts_succeeded": int(parts_succeeded),
            "merge_succeeded": bool(merge_succeeded),
            "duration_ms": int(duration_ms),
        }
        if error:
            record["error"] = str(error)[:300]
        if tokens_before_prune:
            record["tokens_before_prune"] = int(tokens_before_prune)
        if bytes_pruned:
            record["bytes_pruned"] = int(bytes_pruned)

        path = Path(workspace_dir) / EVENTS_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        # Pruning is opportunistic: only every 100th call, to avoid IO churn.
        # The 5000 cap keeps the file under ~1MB.
        try:
            if path.stat().st_size > 1_000_000:
                _prune(path)
        except OSError:
            pass
    except Exception as e:
        logger.warning("compaction event log failed: %s", e)


def _prune(path: Path) -> None:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) <= MAX_EVENTS_ON_DISK:
        return
    keep = lines[-MAX_EVENTS_ON_DISK:]
    path.write_text("\n".join(keep) + "\n", encoding="utf-8")


def load_events(
    workspace_dir: str,
    *,
    days: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Load compaction events. Filter by days (newest N days) or limit."""
    path = Path(workspace_dir) / EVENTS_FILENAME
    if not path.exists():
        return []
    cutoff = None
    if days is not None and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    events: list[dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if cutoff and str(obj.get("ts", "")) < cutoff:
            continue
        events.append(obj)
    events.reverse()  # newest first
    if limit:
        events = events[:limit]
    return events


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate metrics across a list of events. Used by compaction_stats tool."""
    if not events:
        return {"count": 0}
    n = len(events)
    by_stage: dict[str, int] = {}
    total_tokens_in = 0
    total_tokens_out = 0
    total_duration = 0
    errors = 0
    merge_failures = 0
    for e in events:
        stage = str(e.get("stage", "unknown"))
        by_stage[stage] = by_stage.get(stage, 0) + 1
        total_tokens_in += int(e.get("tokens_before", 0) or 0)
        total_tokens_out += int(e.get("tokens_after", 0) or 0)
        total_duration += int(e.get("duration_ms", 0) or 0)
        if e.get("error"):
            errors += 1
        if not e.get("merge_succeeded", True):
            merge_failures += 1
    avg_compression = (
        round(1.0 - (total_tokens_out / total_tokens_in), 3)
        if total_tokens_in > 0 else 0.0
    )
    return {
        "count": n,
        "by_stage": by_stage,
        "errors": errors,
        "error_rate": round(errors / n, 3) if n else 0.0,
        "merge_failures": merge_failures,
        "avg_compression_ratio": avg_compression,
        "avg_duration_ms": int(total_duration / n) if n else 0,
        "avg_tokens_before": int(total_tokens_in / n) if n else 0,
        "avg_tokens_after": int(total_tokens_out / n) if n else 0,
    }
