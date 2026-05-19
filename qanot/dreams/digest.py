"""Deterministic session-transcript digest for the consolidation cron.

The weekly consolidation agent used to read only *daily notes* — a
summary of a summary. The richer signal (what the user actually said,
the corrections they made, the asks that recur) lives in the raw
session JSONL. Anthropic's managed "Dreams" pipeline mines full
transcripts for exactly this reason.

This module does the cheap, deterministic half: it walks the recent
JSONL sessions and renders a bounded, readable digest the consolidation
agent reads as its PRIMARY input. No LLM, no network — same philosophy
as ``dreams/verify.py``: mechanical pre/post passes wrap the LLM core.

The digest is intentionally lossy in a *predictable* way (per-message
truncation + a global char budget, newest sessions first) so it can
never blow the isolated agent's context regardless of history size.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# User message content carries injected scaffolding — recalled-memory
# <system-reminder> blocks, link-preview dumps — that is NOT what the
# user said. Mining it would make consolidation re-learn its own memos
# (circular) and pollute durable-fact extraction. Strip it so the
# digest stays faithful to real user phrasing.
_SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE,
)

# Tight defaults — bound the agent's context no matter how big history is.
DEFAULT_DAYS = 7
DEFAULT_MAX_CHARS = 24_000
PER_MESSAGE_CAP = 600          # chars per single turn before truncation
# Cron/heartbeat/briefing runs are the bot talking to itself — noise for
# consolidation, which is about the *user*. Skip these session files.
_SKIP_PREFIXES = ("cron-",)


def _truncate(text: str, cap: int) -> str:
    text = " ".join(text.split())  # collapse newlines/runs → one line
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _message_text(content: object) -> tuple[str, list[str]]:
    """Return (visible_text, tool_names) for a message ``content`` value.

    User content is a plain string; assistant content is a list of
    text / tool_use blocks (see ``qanot/session.py``). Tolerate both.
    """
    if isinstance(content, str):
        return _SYSTEM_REMINDER_RE.sub("", content).strip(), []
    if not isinstance(content, list):
        return "", []
    texts: list[str] = []
    tools: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text") or ""
            if t.strip():
                texts.append(t.strip())
        elif btype == "tool_use":
            name = block.get("name") or ""
            if name:
                tools.append(name)
    return " ".join(texts).strip(), tools


def _recent_session_files(
    sessions_dir: Path, *, days: int, now: datetime,
) -> list[Path]:
    """Newest-first list of non-cron *.jsonl files modified within ``days``."""
    if not sessions_dir.is_dir():
        return []
    cutoff = now - timedelta(days=days)
    out: list[tuple[float, Path]] = []
    for p in sessions_dir.glob("*.jsonl"):
        if not p.is_file() or p.name.startswith(_SKIP_PREFIXES):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if datetime.fromtimestamp(mtime, tz=timezone.utc) < cutoff:
            continue
        out.append((mtime, p))
    out.sort(key=lambda x: x[0], reverse=True)  # newest first
    return [p for _, p in out]


def build_session_digest(
    sessions_dir: str | Path,
    *,
    days: int = DEFAULT_DAYS,
    max_chars: int = DEFAULT_MAX_CHARS,
    now: datetime | None = None,
) -> str:
    """Render a bounded markdown digest of recent session transcripts.

    Returns ``""`` when there is nothing to digest (missing dir, no
    recent non-cron sessions, or no extractable turns). Never raises —
    a malformed line is skipped, not fatal; consolidation must run even
    if the digest is empty.
    """
    now = now or datetime.now(timezone.utc)
    files = _recent_session_files(Path(sessions_dir), days=days, now=now)
    if not files:
        return ""

    chunks: list[str] = [
        f"# Recent session transcripts (last {days} days)",
        "",
        "Primary signal for consolidation: this is what the user "
        "actually said and did across sessions — raw phrasing, "
        "corrections, repeated asks. Mine it for DURABLE facts.",
        "",
    ]
    total = sum(len(c) + 1 for c in chunks)
    truncated = False

    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        turns: list[str] = []
        first_ts = last_ts = ""
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                entry = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "message":
                continue
            msg = entry.get("message") or {}
            role = msg.get("role") or ""
            text, tools = _message_text(msg.get("content"))
            if not text and not tools:
                continue
            ts = (entry.get("timestamp") or "")[:19]
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            who = "User" if role == "user" else "Asst"
            uid = entry.get("user_id") or ""
            tag = f"{who}({uid})" if (who == "User" and uid) else who
            body = _truncate(text, PER_MESSAGE_CAP) if text else ""
            tool_suffix = f" [tools: {', '.join(tools)}]" if tools else ""
            turns.append(f"- [{ts}] {tag}: {body}{tool_suffix}".rstrip())

        if not turns:
            continue

        span = (
            f"{first_ts[:10]}"
            + (f"→{last_ts[:10]}" if last_ts[:10] != first_ts[:10] else "")
        )
        header = f"## {path.stem} ({len(turns)} turns, {span})"
        block = "\n".join([header, *turns])
        if total + len(block) + 2 > max_chars:
            truncated = True
            break
        chunks.append(block)
        chunks.append("")
        total += len(block) + 2

    if truncated:
        chunks.append(
            "_(older sessions omitted — digest hit the size budget; "
            "newest are shown first)_"
        )

    body = "\n".join(chunks).strip()
    # Header-only output means no real turns were extracted.
    return body if "## " in body else ""


def write_session_digest(
    sessions_dir: str | Path,
    out_path: str | Path,
    *,
    days: int = DEFAULT_DAYS,
    max_chars: int = DEFAULT_MAX_CHARS,
    now: datetime | None = None,
) -> int:
    """Build the digest and write it to ``out_path``.

    Returns the number of characters written (0 when there was nothing
    to digest — in that case the file is left untouched / not created).
    """
    digest = build_session_digest(
        sessions_dir, days=days, max_chars=max_chars, now=now,
    )
    if not digest:
        return 0
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(digest, encoding="utf-8")
    return len(digest)
