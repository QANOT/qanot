"""Anthropic OAuth / rate-limit quota reporting.

Two sources, in priority order:

1. **OAuth usage endpoint** ``GET /api/oauth/usage`` — the real Claude
   *subscription* quota: rolling 5-hour session window, weekly windows
   (incl. per-model), and the ``extra_usage`` credit pool (the lane that
   throws "out of extra usage" 400s). Requires an OAuth token with the
   ``user:profile`` scope — a full Claude Code login, NOT a bare
   inference ``setup-token``. Returns a 403 otherwise, which we surface
   as an honest "scope yetishmaydi" note rather than silently showing
   nothing.

2. **Response headers** ``anthropic-ratelimit-*`` — sent to *API-key*
   accounts on every Messages response. Captured opportunistically by the
   provider as a fallback for non-OAuth deployments.

For OAuth/subscription bots the rolling window is the binding limit, not
dollars — so ``/usage`` and the dashboard show e.g.
"Sessiya (5h): 62% ishlatilgan • 14:00 da yangilanadi".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_FIELDS = ("limit", "remaining", "reset", "used")
_HDR_PREFIX = "anthropic-ratelimit-"
_FETCH_TTL = 60.0  # seconds — don't hammer the usage endpoint


@dataclass
class QuotaWindow:
    name: str
    label: str = ""
    used_pct: float | None = None       # 0..100 (from the usage endpoint)
    remaining: int | None = None        # from response headers
    limit: int | None = None            # from response headers
    reset_at: str | None = None         # RFC3339 timestamp

    @property
    def remaining_pct(self) -> int | None:
        if self.used_pct is not None:
            return max(0, min(100, round(100 - self.used_pct)))
        if self.limit and self.limit > 0 and self.remaining is not None:
            return max(0, min(100, round(self.remaining / self.limit * 100)))
        return None

    @property
    def reset_in_seconds(self) -> int | None:
        if not self.reset_at:
            return None
        try:
            dt = datetime.fromisoformat(self.reset_at.replace("Z", "+00:00"))
            return max(0, int(dt.timestamp() - time.time()))
        except (ValueError, TypeError):
            return None


@dataclass
class QuotaSnapshot:
    windows: dict[str, QuotaWindow] = field(default_factory=dict)
    captured_at: float = 0.0
    source: str = "headers"             # "oauth_usage_api" | "headers"
    retry_after: int | None = None      # seconds, from a 429
    extra_usage: str | None = None      # "Extra usage: 1.20 / 5.00 USD"
    unavailable_reason: str | None = None

    @property
    def age_seconds(self) -> int:
        return int(time.time() - self.captured_at) if self.captured_at else 0


# Header-captured snapshot (API-key accounts) + cached endpoint fetch.
_LATEST_HEADERS: QuotaSnapshot | None = None
_FETCH_CACHE: tuple[float, QuotaSnapshot | None] = (0.0, None)


def _to_int(v: Any) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# ── 1. OAuth usage endpoint ───────────────────────────────────────────

_WINDOW_LABELS = (
    ("five_hour", "Sessiya (5 soat)"),
    ("seven_day", "Hafta"),
    ("seven_day_opus", "Opus (hafta)"),
    ("seven_day_sonnet", "Sonnet (hafta)"),
)


def _is_oauth(token: str) -> bool:
    return bool(token) and token.startswith("sk-ant-oat")


async def fetch_oauth_usage(token: str | None) -> QuotaSnapshot | None:
    """Fetch subscription quota from the OAuth usage endpoint (cached ``_FETCH_TTL``).

    Returns None for non-OAuth tokens (use the header snapshot instead).
    On a scope/permission error returns a snapshot with ``unavailable_reason``.
    """
    global _FETCH_CACHE
    if not _is_oauth(token or ""):
        return None
    now = time.time()
    if now - _FETCH_CACHE[0] < _FETCH_TTL and _FETCH_CACHE[1] is not None:
        return _FETCH_CACHE[1]

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.0",
    }
    snap: QuotaSnapshot
    try:
        import httpx
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(_USAGE_URL, headers=headers)
        if resp.status_code == 403:
            snap = QuotaSnapshot(
                captured_at=now, source="oauth_usage_api",
                unavailable_reason=(
                    "OAuth token huquqi yetarli emas (user:profile scope). "
                    "Limitni ko'rish uchun to'liq Claude Code login kerak."
                ),
            )
        elif resp.status_code >= 400:
            snap = QuotaSnapshot(
                captured_at=now, source="oauth_usage_api",
                unavailable_reason=f"Usage endpoint xatosi ({resp.status_code}).",
            )
        else:
            snap = _parse_usage_payload(resp.json() or {}, now)
    except Exception as e:  # noqa: BLE001 — never raise into /usage
        logger.debug("oauth usage fetch failed: %s", e)
        snap = QuotaSnapshot(captured_at=now, source="oauth_usage_api",
                             unavailable_reason="Usage endpointga ulanib bo'lmadi.")
    _FETCH_CACHE = (now, snap)
    return snap


def _parse_usage_payload(payload: dict, now: float) -> QuotaSnapshot:
    windows: dict[str, QuotaWindow] = {}
    for key, label in _WINDOW_LABELS:
        w = payload.get(key) or {}
        util = w.get("utilization")
        if util is None:
            continue
        try:
            util = float(util)
        except (TypeError, ValueError):
            continue
        used_pct = util * 100 if util <= 1 else util
        windows[key] = QuotaWindow(
            name=key, label=label, used_pct=used_pct,
            reset_at=w.get("resets_at") or w.get("reset_at"),
        )
    extra_usage = None
    extra = payload.get("extra_usage") or {}
    if extra.get("is_enabled"):
        used = extra.get("used_credits")
        limit = extra.get("monthly_limit")
        cur = extra.get("currency") or "USD"
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)):
            extra_usage = f"Extra usage: {used:.2f} / {limit:.2f} {cur}"
    return QuotaSnapshot(
        windows=windows, captured_at=now, source="oauth_usage_api",
        extra_usage=extra_usage,
        unavailable_reason=None if windows else "Limit ma'lumoti bo'sh qaytdi.",
    )


# ── 2. Response-header capture (API-key accounts) ─────────────────────

def update_from_headers(headers: Any) -> None:
    """Parse ``anthropic-ratelimit-*`` headers into the global header snapshot."""
    global _LATEST_HEADERS
    try:
        if not hasattr(headers, "items"):
            return
        items: Iterable[tuple[str, str]] = headers.items()
        windows: dict[str, QuotaWindow] = {}
        retry_after: int | None = None
        for raw_key, value in items:
            key = str(raw_key).lower()
            if key == "retry-after":
                retry_after = _to_int(value)
                continue
            if not key.startswith(_HDR_PREFIX):
                continue
            rest = key[len(_HDR_PREFIX):]
            field_name = next((f for f in _FIELDS if rest.endswith("-" + f)), None)
            if not field_name:
                continue
            win_name = rest[: -(len(field_name) + 1)] or "default"
            win = windows.setdefault(win_name, QuotaWindow(name=win_name, label=win_name))
            if field_name == "reset":
                win.reset_at = str(value)
            elif field_name == "limit":
                win.limit = _to_int(value)
            elif field_name == "remaining":
                win.remaining = _to_int(value)
        if not windows and retry_after is None:
            return
        _LATEST_HEADERS = QuotaSnapshot(
            windows=windows, captured_at=time.time(),
            source="headers", retry_after=retry_after,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("rate-limit header parse failed: %s", e)


def latest_headers() -> QuotaSnapshot | None:
    return _LATEST_HEADERS


# ── Formatting ────────────────────────────────────────────────────────

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


def format_report(snap: QuotaSnapshot | None) -> str:
    """Human-readable Uzbek quota report for a snapshot, or '' when empty."""
    if snap is None:
        return ""
    if snap.unavailable_reason and not snap.windows:
        return f"**OAuth limit:** {snap.unavailable_reason}"
    if not snap.windows and not snap.extra_usage:
        return ""
    lines = ["**OAuth limit (hisob bo'yicha):**"]
    for w in snap.windows.values():
        bits: list[str] = []
        if w.used_pct is not None:
            bits.append(f"{round(w.used_pct)}% ishlatilgan")
        elif w.remaining is not None and w.limit is not None:
            bits.append(f"{w.remaining:,}/{w.limit:,} qoldi")
        rs = w.reset_in_seconds
        if rs is not None:
            bits.append(f"• {_human_duration(rs)}dan keyin yangilanadi")
        label = w.label or w.name
        lines.append(f"{label}: " + " ".join(bits))
    if snap.extra_usage:
        lines.append(snap.extra_usage)
    if snap.retry_after:
        lines.append(f"⚠️ Limit: {_human_duration(snap.retry_after)} kuting")
    return "\n".join(lines)


async def build_usage_report(token: str | None) -> str:
    """End-to-end: try the OAuth endpoint, fall back to captured headers."""
    snap = await fetch_oauth_usage(token)
    report = format_report(snap)
    if report:
        return report
    return format_report(latest_headers())
