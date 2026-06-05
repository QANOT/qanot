"""Usage insights — 30-day tool / cost / model / activity trends.

Qanot already records per-turn data in the session JSONL (the same files
``cache_stats`` reads for cache health), but never aggregates it into an
operator-facing trend report: which tools dominate token spend, cost by
model, when the bot is busiest, activity streaks. This module does that walk
and ``/insights`` + the dashboard surface it.

Pure JSONL aggregation (qanot has no session SQLite DB) — tolerant of corrupt
lines, scoped by a day window via per-entry timestamp.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def generate_insights(sessions_dir: str | Path, *, days: int = 30) -> dict[str, Any]:
    """Aggregate the last ``days`` of session transcripts into a trend report."""
    root = Path(sessions_dir)
    if not root.exists():
        return {"days": days, "empty": True}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = cutoff.timestamp()

    turns = 0
    tokens = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    cost_usd = 0.0
    tool_counts: Counter[str] = Counter()
    model_cost: dict[str, float] = {}
    model_turns: Counter[str] = Counter()
    per_day: Counter[str] = Counter()
    per_hour: Counter[int] = Counter()
    active_dates: set[str] = set()
    sessions_seen: set[str] = set()
    users_seen: set[str] = set()

    for path in root.glob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff_ts:
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
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
            ts = _parse_ts(obj.get("timestamp"))
            if ts is not None and ts < cutoff:
                continue

            # Tool usage — count tool_use blocks in assistant content.
            msg = obj.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name")
                        if name:
                            tool_counts[name] += 1

            # Activity buckets (any message with a timestamp).
            if ts is not None:
                day_key = ts.date().isoformat()
                per_day[day_key] += 1
                per_hour[ts.hour] += 1
                active_dates.add(day_key)
            sessions_seen.add(path.stem)
            if obj.get("user_id"):
                users_seen.add(str(obj["user_id"]))

            # Token/cost — only assistant turns carry usage.
            usage = obj.get("usage")
            if not isinstance(usage, dict):
                continue
            turns += 1
            for k in tokens:
                tokens[k] += int(usage.get(k, 0) or 0)
            cost = usage.get("cost")
            turn_cost = 0.0
            if isinstance(cost, dict):
                try:
                    turn_cost = float(cost.get("total", 0) or 0)
                except (TypeError, ValueError):
                    turn_cost = 0.0
            cost_usd += turn_cost
            model = obj.get("model") or "unknown"
            model_cost[model] = model_cost.get(model, 0.0) + turn_cost
            model_turns[model] += 1

    streak = _current_streak(active_dates)
    busiest_hour = per_hour.most_common(1)[0][0] if per_hour else None

    return {
        "days": days,
        "empty": turns == 0 and not tool_counts,
        "overview": {
            "turns": turns,
            "sessions": len(sessions_seen),
            "active_days": len(active_dates),
            "users": len(users_seen),
            "cost_usd": round(cost_usd, 4),
            "tokens": tokens,
        },
        "top_tools": tool_counts.most_common(10),
        "by_model": [
            {"model": m, "turns": model_turns[m], "cost_usd": round(c, 4)}
            for m, c in sorted(model_cost.items(), key=lambda kv: -kv[1])
        ],
        "activity": {
            "busiest_hour_utc": busiest_hour,
            "current_streak_days": streak,
            "per_day": dict(sorted(per_day.items())),
        },
    }


def _current_streak(active_dates: set[str]) -> int:
    """Consecutive active days ending today or yesterday."""
    if not active_dates:
        return 0
    today = date.today()
    # Allow the streak to "end" yesterday (today may not have activity yet).
    cursor = today if today.isoformat() in active_dates else today - timedelta(days=1)
    streak = 0
    while cursor.isoformat() in active_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def format_insights(data: dict[str, Any]) -> str:
    """Render an insights report as Uzbek markdown for Telegram."""
    if data.get("empty"):
        return f"📊 So'nggi {data.get('days', 30)} kunda ma'lumot yo'q."
    ov = data["overview"]
    tk = ov["tokens"]
    lines = [
        f"📊 **Insights — so'nggi {data['days']} kun**",
        "",
        f"Navbatlar: {ov['turns']:,} • Sessiyalar: {ov['sessions']} • "
        f"Faol kunlar: {ov['active_days']}",
        f"Xarajat: ${ov['cost_usd']:.2f} • Tokenlar: "
        f"{tk['input'] + tk['output']:,} (kesh o'qish: {tk['cacheRead']:,})",
    ]
    act = data["activity"]
    if act.get("current_streak_days"):
        lines.append(f"🔥 Ketma-ket faol: {act['current_streak_days']} kun")
    if act.get("busiest_hour_utc") is not None:
        lines.append(f"⏰ Eng band soat (UTC): {act['busiest_hour_utc']:02d}:00")

    if data["top_tools"]:
        lines.append("\n**Eng ko'p ishlatilgan toollar:**")
        for name, n in data["top_tools"][:7]:
            lines.append(f"• {name}: {n}×")

    if data["by_model"]:
        lines.append("\n**Model bo'yicha:**")
        for m in data["by_model"][:5]:
            lines.append(f"• {m['model']}: {m['turns']} navbat (${m['cost_usd']:.2f})")

    return "\n".join(lines)
