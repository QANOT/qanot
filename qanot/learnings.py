"""Self-improvement learnings store.

Bidirectional learning loop. The agent captures observations into a
typed, append-only JSONL via `evolve_soul`, and recent learnings
auto-inject into the system prompt at session start. Closes the
write-only daily-notes gap from the audit (agent never read its own
past errors → now the most recent lessons are always in context).

Storage: <workspace>/memory/learnings.jsonl
  One JSON object per line, append-only.
  Schema: {ts, user_id, observation, lesson, tags}

Bounds:
  - Max entries on disk: 1000 (oldest pruned on overflow)
  - Max bytes per entry: 1024 (tool enforces)
  - Max observation: 500 chars (one sentence of context)
  - Max lesson: 400 chars (one actionable sentence)
  - Tags: up to 10, normalized to lowercase

SOUL.md remains untouched. Identity is sacred; lessons are
empirical observations. Tool name `evolve_soul` is metaphorical —
it doesn't actually mutate SOUL.md, it appends to a separate ledger
the agent can read back.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LEARNINGS_FILENAME = "memory/learnings.jsonl"
MAX_ENTRIES_ON_DISK = 1000
MAX_BYTES_PER_ENTRY = 1024
MAX_OBSERVATION_LEN = 500
MAX_LESSON_LEN = 400
MAX_TAGS_PER_ENTRY = 10
MAX_TAG_LEN = 40


def _normalize_tags(tags: Any) -> list[str]:
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in tags[:MAX_TAGS_PER_ENTRY]:
        if not isinstance(t, str):
            continue
        normalized = t.strip().lower()[:MAX_TAG_LEN]
        if normalized and normalized not in seen:
            out.append(normalized)
            seen.add(normalized)
    return out


def append_learning(
    workspace_dir: str,
    observation: str,
    lesson: str,
    *,
    tags: list[str] | None = None,
    user_id: str = "",
) -> dict[str, Any]:
    """Append one learning entry. Returns the persisted dict.

    Raises ValueError on invalid input (caller surfaces to the agent
    so it can correct the call rather than silently dropping data).
    """
    obs = (observation or "").strip()
    les = (lesson or "").strip()
    if not obs:
        raise ValueError("observation is required (what happened — one sentence)")
    if not les:
        raise ValueError("lesson is required (the actionable takeaway — one sentence)")
    if len(obs) > MAX_OBSERVATION_LEN:
        raise ValueError(f"observation too long (max {MAX_OBSERVATION_LEN} chars, got {len(obs)})")
    if len(les) > MAX_LESSON_LEN:
        raise ValueError(f"lesson too long (max {MAX_LESSON_LEN} chars, got {len(les)})")

    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_id": (user_id or "").strip()[:64],
        "observation": obs,
        "lesson": les,
        "tags": _normalize_tags(tags),
    }
    line = json.dumps(entry, ensure_ascii=False)
    if len(line.encode("utf-8")) > MAX_BYTES_PER_ENTRY:
        raise ValueError(f"entry too large after serialization (max {MAX_BYTES_PER_ENTRY} bytes)")

    path = Path(workspace_dir) / LEARNINGS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")

    _prune_if_needed(path)
    return entry


def _prune_if_needed(path: Path) -> None:
    """Drop oldest entries if we're over the cap. Preserves order."""
    if not path.exists():
        return
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) <= MAX_ENTRIES_ON_DISK:
        return
    keep = lines[-MAX_ENTRIES_ON_DISK:]
    path.write_text("\n".join(keep) + "\n", encoding="utf-8")


def load_learnings(workspace_dir: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Load all learnings, newest first. Tolerates corrupt lines (skips them)."""
    path = Path(workspace_dir) / LEARNINGS_FILENAME
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("lesson"):
            entries.append(obj)
    entries.reverse()  # newest first
    if limit is not None and limit > 0:
        entries = entries[:limit]
    return entries


def search_learnings(
    workspace_dir: str,
    topic: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Substring search over observation + lesson + tags. Empty topic = most recent."""
    if not topic:
        return load_learnings(workspace_dir, limit=limit)
    topic_lower = topic.strip().lower()
    if not topic_lower:
        return load_learnings(workspace_dir, limit=limit)
    matches: list[dict[str, Any]] = []
    for e in load_learnings(workspace_dir):
        searchable = " ".join([
            str(e.get("observation", "")),
            str(e.get("lesson", "")),
            " ".join(e.get("tags", []) if isinstance(e.get("tags"), list) else []),
        ]).lower()
        if topic_lower in searchable:
            matches.append(e)
        if len(matches) >= limit:
            break
    return matches


def format_recent_learnings_block(
    workspace_dir: str,
    *,
    limit: int = 5,
) -> str:
    """Build the system-prompt section for recent learnings.

    Returns "" when there are no learnings (don't bloat empty prompts).
    Format is intentionally compact — each lesson is one line, tags
    abbreviated. ~5 entries × ~120 chars = ~600 chars / ~150 tokens.
    """
    learnings = load_learnings(workspace_dir, limit=limit)
    if not learnings:
        return ""
    lines = [
        "# Recent Learnings (your past lessons)",
        "",
        "Lessons you captured from past interactions, newest first. "
        "When approaching a similar problem, follow what you learned. "
        "Use `recall_lessons` for older or topic-specific recall.",
        "",
    ]
    for e in learnings:
        date = str(e.get("ts", ""))[:10]  # YYYY-MM-DD
        lesson = str(e.get("lesson", "")).strip()
        tags = e.get("tags", []) or []
        tag_str = ""
        if isinstance(tags, list) and tags:
            tag_str = f" [{', '.join(str(t) for t in tags[:3])}]"
        lines.append(f"- {date}: {lesson}{tag_str}")
    return "\n".join(lines)


def extract_recent_error_lessons(
    workspace_dir: str,
    *,
    days: int = 7,
    max_entries: int = 5,
) -> list[dict[str, str]]:
    """Pull error-flavored entries from recent daily notes.

    The audit said daily notes are write-only (agent logs errors but
    never reads them back). This injects them at session start so the
    bidirectional loop closes.

    Heuristic: looks for lines containing common error markers. Daily
    notes don't have a hard "error" tag, so we use substring matching.
    Returns at most max_entries, newest first.
    """
    ws = Path(workspace_dir)
    memory_dir = ws / "memory"
    if not memory_dir.exists():
        return []

    today = datetime.now(timezone.utc).date()
    relevant_dates = [
        (today - timedelta(days=d)).isoformat()
        for d in range(days)
    ]

    error_markers = (
        "error lesson",
        "loop detected",
        "loop-detection",
        "no progress",
        "tool failed",
        "[error]",
        "exception:",
        "failed:",
    )

    out: list[dict[str, str]] = []
    for date_str in relevant_dates:
        if len(out) >= max_entries:
            break
        note = memory_dir / f"{date_str}.md"
        if not note.exists():
            continue
        try:
            content = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for chunk in _extract_error_chunks(content, error_markers):
            out.append({"date": date_str, "content": chunk})
            if len(out) >= max_entries:
                break
    return out


def _extract_error_chunks(content: str, markers: tuple[str, ...]) -> list[str]:
    """Pull lines (with 1 line of context) that match any error marker."""
    chunks: list[str] = []
    lines = content.splitlines()
    seen_starts: set[int] = set()
    for i, line in enumerate(lines):
        lower = line.lower()
        if not any(m in lower for m in markers):
            continue
        # Skip if we already grabbed an overlapping chunk.
        if i in seen_starts:
            continue
        # Take this line + up to 2 trailing context lines, trimmed.
        block = "\n".join(lines[i:i + 3]).strip()
        if block:
            chunks.append(block)
        seen_starts.update(range(i, i + 3))
        if len(chunks) >= 10:
            break
    return chunks


def format_error_lessons_block(workspace_dir: str, *, days: int = 7) -> str:
    """System-prompt section for recent error lessons. "" if none."""
    lessons = extract_recent_error_lessons(workspace_dir, days=days)
    if not lessons:
        return ""
    lines = [
        "# Recent Error Notes (last 7 days)",
        "",
        "Past failures you logged. Skim before attempting similar actions.",
        "",
    ]
    for el in lessons:
        snippet = el["content"].replace("\n", " ").strip()[:200]
        lines.append(f"- [{el['date']}] {snippet}")
    return "\n".join(lines)
