"""Capture Anthropic rate-limit / OAuth quota from API response headers.

Anthropic returns ``anthropic-ratelimit-*`` headers on every Messages
response. For OAuth / Claude-subscription bots the *rolling usage window*
(requests / tokens, sometimes a unified window) is the operative limit —
not dollars — so we snapshot the latest headers process-globally. The
account is a single OAuth identity per process, so one global snapshot is
correct; ``/usage`` and the dashboard read it to show
"Session: 38% qoldi • ~2 soatdan keyin yangilanadi".

We parse generically (any ``anthropic-ratelimit-<window>-<field>`` header)
so new window names Anthropic introduces are picked up without a code change.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_FIELDS = ("limit", "remaining", "reset", "used")
_PREFIX = "anthropic-ratelimit-"


@dataclass
class QuotaWindow:
    """One rate-limit window (e.g. requests, tokens, unified-5h)."""

    name: str
    limit: int | None = None
    remaining: int | None = None
    reset: str | None = None  # RFC3339 timestamp string from the header

    @property
    def remaining_pct(self) -> int | None:
        if self.limit and self.limit > 0 and self.remaining is not None:
            return max(0, min(100, round(self.remaining / self.limit * 100)))
        return None

    @property
    def reset_in_seconds(self) -> int | None:
        if not self.reset:
            return None
        try:
            dt = datetime.fromisoformat(self.reset.replace("Z", "+00:00"))
            return max(0, int(dt.timestamp() - time.time()))
        except (ValueError, TypeError):
            return None


@dataclass
class QuotaSnapshot:
    windows: dict[str, QuotaWindow] = field(default_factory=dict)
    captured_at: float = 0.0
    retry_after: int | None = None  # seconds, set on a 429

    @property
    def age_seconds(self) -> int:
        return int(time.time() - self.captured_at) if self.captured_at else 0


_LATEST: QuotaSnapshot | None = None


def _to_int(v: Any) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def update_from_headers(headers: Any) -> None:
    """Parse ``anthropic-ratelimit-*`` headers into the global snapshot.

    ``headers`` is any mapping (httpx.Headers, dict). Best-effort: never
    raises into the request path.
    """
    global _LATEST
    try:
        items: Iterable[tuple[str, str]]
        if hasattr(headers, "items"):
            items = headers.items()
        else:
            return
        windows: dict[str, QuotaWindow] = {}
        retry_after: int | None = None
        for raw_key, value in items:
            key = str(raw_key).lower()
            if key == "retry-after":
                retry_after = _to_int(value)
                continue
            if not key.startswith(_PREFIX):
                continue
            rest = key[len(_PREFIX):]  # e.g. "requests-remaining", "unified-5h-reset"
            field_name = next((f for f in _FIELDS if rest.endswith("-" + f)), None)
            if not field_name:
                continue
            win_name = rest[: -(len(field_name) + 1)] or "default"
            win = windows.setdefault(win_name, QuotaWindow(name=win_name))
            if field_name == "reset":
                win.reset = str(value)
            elif field_name == "limit":
                win.limit = _to_int(value)
            elif field_name == "remaining":
                win.remaining = _to_int(value)
            # "used" is derivable from limit-remaining; ignore to avoid drift.
        if not windows and retry_after is None:
            return
        _LATEST = QuotaSnapshot(
            windows=windows, captured_at=time.time(), retry_after=retry_after,
        )
    except Exception as e:  # noqa: BLE001 — never break the API call
        logger.debug("rate-limit header parse failed: %s", e)


def latest() -> QuotaSnapshot | None:
    return _LATEST


def _human_duration(secs: int) -> str:
    if secs <= 0:
        return "hozir"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h and m:
        return f"{h} soat {m} daqiqa"
    if h:
        return f"{h} soat"
    if m:
        return f"{m} daqiqa"
    return f"{secs} soniya"


# Windows worth showing first, in this order (others appended after).
_PREFERRED = ("unified", "requests", "tokens", "input-tokens", "output-tokens")


def format_report() -> str:
    """Human-readable Uzbek quota report, or '' when no data captured yet."""
    snap = _LATEST
    if snap is None or not snap.windows:
        return ""
    order = sorted(
        snap.windows.values(),
        key=lambda w: next(
            (i for i, p in enumerate(_PREFERRED) if w.name.startswith(p)), 99
        ),
    )
    lines = ["**OAuth limit (hisob bo'yicha):**"]
    for w in order:
        pct = w.remaining_pct
        bits: list[str] = []
        if w.remaining is not None and w.limit is not None:
            bits.append(f"{w.remaining:,}/{w.limit:,} qoldi")
        elif w.remaining is not None:
            bits.append(f"{w.remaining:,} qoldi")
        if pct is not None:
            bits.append(f"({pct}%)")
        rs = w.reset_in_seconds
        if rs is not None:
            bits.append(f"• {_human_duration(rs)}dan keyin yangilanadi")
        lines.append(f"{w.name}: " + " ".join(bits))
    if snap.retry_after:
        lines.append(f"⚠️ Limit: {_human_duration(snap.retry_after)} kuting")
    age = snap.age_seconds
    freshness = "hozirgina yangilandi" if age < 5 else f"{_human_duration(age)} oldin yangilangan"
    lines.append(f"_({freshness})_")
    return "\n".join(lines)
