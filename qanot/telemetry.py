"""Per-call LLM telemetry — JSONL sink for real-traffic analysis.

Captures every provider.chat() call so future flag-flip decisions
(inject_legacy_memory, context_editing_enabled, model tier, …) are
grounded in production data instead of synthetic benchmarks. See
`scripts/bench_memory.py` for the synthetic path this complements.

Design:
  - Writes `{workspace_dir}/logs/calls-YYYY-MM-DD.jsonl`, one JSON
    line per call. Daily rotation so old data stays easy to diff.
  - Best-effort: any write failure is swallowed so it can't break
    the agent loop.
  - Module-level singleton initialised by main.py. Provider code
    calls `record_call(...)` and doesn't care whether logging is on.

Analysis:
    jq -s '
      map(select(.config.context_editing_enabled))
      | {n: length,
         mean_in: (map(.input_tokens) | add / length),
         p95_lat: (map(.latency_ms) | sort | .[(length * 0.95) | floor])}
    ' /data/workspace/logs/calls-2026-04-22.jsonl
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TelemetryLogger:
    """Append-only JSONL writer. Thread-safe via a per-file lock."""

    def __init__(self, workspace_dir: str) -> None:
        self.logs_dir = Path(workspace_dir) / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path_for_today(self) -> Path:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        return self.logs_dir / f"calls-{today}.jsonl"

    def record(self, event: dict[str, Any]) -> None:
        """Append one event. Swallows all exceptions — never disrupts caller."""
        try:
            line = json.dumps(event, ensure_ascii=False, default=str)
            path = self._path_for_today()
            with self._lock, path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
        except Exception as e:
            logger.debug("Telemetry write failed (non-fatal): %s", e)


_default: TelemetryLogger | None = None


def init(workspace_dir: str) -> TelemetryLogger:
    """Initialise the process-wide telemetry logger."""
    global _default
    _default = TelemetryLogger(workspace_dir)
    logger.info("Telemetry logger active at %s", _default.logs_dir)
    return _default


def record_call(**event: Any) -> None:
    """Record one provider call. No-op when telemetry isn't initialised."""
    if _default is None:
        return
    if "ts" not in event:
        event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _default.record(event)
