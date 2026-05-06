"""Per-user token and cost tracking.

Tracks input/output tokens, cache hits, and estimated cost per user.
Persists to a JSON file so costs survive restarts.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class CostTracker:
    """Per-user token and cost tracking.

    Tracks input/output tokens, cache hits, and estimated cost per user.
    Persists to a JSON file so costs survive restarts.
    """

    def __init__(self, workspace_dir: str = "/data/workspace"):
        self.workspace_dir = Path(workspace_dir)
        self._users: dict[str, dict] = {}
        self._load()

    def _cost_file(self) -> Path:
        return self.workspace_dir / "costs.json"

    def _load(self) -> None:
        """Load persisted cost data."""
        path = self._cost_file()
        if path.exists():
            try:
                self._users = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load costs.json: %s", e)
                self._users = {}

    def _save(self) -> None:
        """Persist cost data to disk (atomic write)."""
        from qanot.utils import atomic_write
        path = self._cost_file()
        try:
            atomic_write(path, json.dumps(self._users, indent=2))
        except OSError as e:
            logger.warning("Failed to save costs.json: %s", e)

    def _ensure_user(self, user_id: str) -> dict:
        if user_id not in self._users:
            self._users[user_id] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_cost": 0.0,
                "api_calls": 0,
                "turns": 0,
                "daily_cost": 0.0,
                "daily_date": "",
            }
        return self._users[user_id]

    def _reset_daily_if_needed(self, user: dict) -> None:
        """Reset daily cost if the date has changed."""
        from datetime import date
        today = date.today().isoformat()
        if user.get("daily_date") != today:
            user["daily_cost"] = 0.0
            user["daily_date"] = today

    def add_usage(
        self,
        user_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_write: int = 0,
        cost: float = 0.0,
    ) -> None:
        """Record token usage and cost for a user from a single API call."""
        u = self._ensure_user(user_id)
        u["input_tokens"] += input_tokens
        u["output_tokens"] += output_tokens
        u["cache_read_tokens"] += cache_read
        u["cache_write_tokens"] += cache_write
        u["total_cost"] += cost
        u["api_calls"] += 1
        # Daily tracking
        self._reset_daily_if_needed(u)
        u["daily_cost"] = u.get("daily_cost", 0.0) + cost

    def check_budget(self, user_id: str, daily_budget: float) -> tuple[bool, float, float]:
        """Check if user is within daily budget.

        Returns (allowed, daily_spent, daily_budget).
        If daily_budget <= 0, always allowed (unlimited).
        """
        if daily_budget <= 0:
            return True, 0.0, 0.0
        u = self._ensure_user(user_id)
        self._reset_daily_if_needed(u)
        spent = u.get("daily_cost", 0.0)
        return spent < daily_budget, spent, daily_budget

    def get_budget_warning(self, user_id: str, daily_budget: float, warning_pct: int = 80) -> str | None:
        """Return a warning message if user is near budget limit. None if OK."""
        if daily_budget <= 0:
            return None
        u = self._ensure_user(user_id)
        self._reset_daily_if_needed(u)
        spent = u.get("daily_cost", 0.0)
        pct = (spent / daily_budget) * 100 if daily_budget > 0 else 0
        if pct >= 100:
            return f"Kunlik budget tugadi (${spent:.4f} / ${daily_budget:.2f}). Ertaga qayta urinib ko'ring."
        if pct >= warning_pct:
            return f"Kunlik budgetning {pct:.0f}% ishlatildi (${spent:.4f} / ${daily_budget:.2f})."
        return None

    def add_turn(self, user_id: str) -> None:
        """Increment turn count for a user."""
        self._ensure_user(user_id)["turns"] += 1

    # ── Per-turn budget tracking ──────────────────────────────
    # Separate from cumulative daily/total — `_turn_state` is reset
    # at the start of each turn (see start_turn) and accumulates as
    # iterations run. Used to detect runaway loops within a single
    # turn (the "$4,200 in 63 hours" failure mode).

    def __post_init__(self) -> None:
        # Older instances loaded without this attribute via JSON state;
        # initialize lazily via _ensure_turn_state.
        pass

    def _ensure_turn_state(self) -> dict:
        if not hasattr(self, "_turn_state"):
            self._turn_state: dict[str, dict] = {}
        return self._turn_state

    def start_turn(self, user_id: str) -> None:
        """Reset per-turn counters at the beginning of a turn."""
        self._ensure_turn_state()
        self._turn_state[user_id] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read": 0,
            "cache_write": 0,
            "cost": 0.0,
            "iterations": 0,
        }

    def record_iteration(
        self,
        user_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_write: int = 0,
        cost: float = 0.0,
    ) -> None:
        """Record one iteration's usage into the per-turn accumulator."""
        self._ensure_turn_state()
        s = self._turn_state.setdefault(user_id, {
            "input_tokens": 0, "output_tokens": 0, "cache_read": 0,
            "cache_write": 0, "cost": 0.0, "iterations": 0,
        })
        s["input_tokens"] += input_tokens
        s["output_tokens"] += output_tokens
        s["cache_read"] += cache_read
        s["cache_write"] += cache_write
        s["cost"] += cost
        s["iterations"] += 1

    def get_turn_total_tokens(self, user_id: str) -> int:
        """Tokens billed this turn (input + output + cache_write).

        cache_read excluded — it's the cheap path, not the cost driver
        we're guarding against. The "runaway loop" failure mode burns
        new input/output tokens repeatedly.
        """
        self._ensure_turn_state()
        s = self._turn_state.get(user_id, {})
        return (
            int(s.get("input_tokens", 0))
            + int(s.get("output_tokens", 0))
            + int(s.get("cache_write", 0))
        )

    def get_turn_cost(self, user_id: str) -> float:
        self._ensure_turn_state()
        s = self._turn_state.get(user_id, {})
        return float(s.get("cost", 0.0))

    def check_per_turn_caps(
        self,
        user_id: str,
        *,
        max_tokens: int = 0,
        max_cost_usd: float = 0.0,
    ) -> tuple[bool, str]:
        """Return (within_limits, reason_if_exceeded).

        Either limit being 0 disables that specific check. When both
        are 0 returns (True, "").
        """
        if max_tokens <= 0 and max_cost_usd <= 0.0:
            return True, ""
        if max_tokens > 0:
            tokens = self.get_turn_total_tokens(user_id)
            if tokens >= max_tokens:
                return False, (
                    f"per-turn token cap exceeded ({tokens:,} ≥ {max_tokens:,}). "
                    "Aborting loop to prevent runaway cost."
                )
        if max_cost_usd > 0.0:
            cost = self.get_turn_cost(user_id)
            if cost >= max_cost_usd:
                return False, (
                    f"per-turn cost cap exceeded (${cost:.4f} ≥ ${max_cost_usd:.4f}). "
                    "Aborting loop."
                )
        return True, ""

    def get_user_stats(self, user_id: str) -> dict:
        """Get cost stats for a specific user."""
        return dict(self._ensure_user(user_id))

    def get_all_stats(self) -> dict[str, dict]:
        """Get cost stats for all users."""
        return {uid: dict(data) for uid, data in self._users.items()}

    def get_total_cost(self) -> float:
        """Get total cost across all users."""
        return sum(u.get("total_cost", 0.0) for u in self._users.values())

    def save(self) -> None:
        """Public save — call periodically or on shutdown."""
        self._save()
