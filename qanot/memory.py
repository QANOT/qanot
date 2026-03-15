"""3-tier memory system with WAL protocol and working buffer.

Memory architecture (OpenClaw-style):
- All memory files are per-agent (shared at workspace root)
- MEMORY.md, SESSION-STATE.md, daily notes — all shared
- Entries tagged with user_id so bot knows who said what
- Conversation history isolation happens at session layer (agent._conversations)
- Privacy is behavioral — bot decides not to share based on context
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Write hooks for memory change notifications ──
_write_hooks: list[Callable] = []


def add_write_hook(hook: Callable[[str, str], None]) -> None:
    """Register a callback for memory writes. Called with (content, source)."""
    _write_hooks.append(hook)


def _notify_hooks(content: str, source: str) -> None:
    """Notify all registered write hooks, catching exceptions."""
    for hook in _write_hooks:
        try:
            hook(content, source)
        except Exception as e:
            logger.warning("Memory write hook failed: %s", e)


# WAL trigger patterns (English + Uzbek)
WAL_PATTERNS = [
    # Corrections (EN + UZ)
    (r"(?:it'?s|actually|no,?\s*i\s*meant|not\s+\w+,?\s+(?:but|it'?s))", "correction"),
    (r"(?:yo'q|aslida|men\s+aytmoqchi|to'g'ri\s+emas)", "correction"),
    # Proper nouns (capitalized words after common intros)
    (r"(?:my\s+name\s+is|i'?m|call\s+me|this\s+is)\s+([A-Z][a-z]+)", "proper_noun"),
    (r"(?:mening?\s+ismim|men\s+)\s*([A-Z][a-z]+)", "proper_noun"),
    (r"(?:sen(?:i|ing)?\s+(?:isming|nom))\s+(\w+)", "proper_noun"),
    # Preferences (EN + UZ)
    (r"(?:i\s+(?:like|prefer|want|don'?t\s+like|hate|love))", "preference"),
    (r"(?:men\s+(?:yoqtiraman|xohlayman|istardim|yomon\s+ko'raman))", "preference"),
    # Decisions (EN + UZ)
    (r"(?:let'?s\s+(?:do|go|use|try)|go\s+with|use\s+)", "decision"),
    (r"(?:qani|keling|ishlataylik|sinab\s+ko'raylik)", "decision"),
    # Specific values
    (r"(?:\d{4}[-/]\d{2}[-/]\d{2}|https?://\S+|\b\d{5,}\b)", "specific_value"),
    # Remember commands (EN + UZ)
    (r"(?:remember\s+(?:this|that)|don'?t\s+forget|eslab\s+qol|unutma|yodda\s+tut)", "remember"),
]

# Patterns that should also be saved to MEMORY.md (durable facts)
DURABLE_CATEGORIES = {"proper_noun", "preference", "remember"}


class WALEntry:
    """A single WAL entry to write."""

    def __init__(self, category: str, detail: str):
        self.category = category
        self.detail = detail
        self.timestamp = datetime.now(timezone.utc).isoformat()


def wal_scan(user_message: str) -> list[WALEntry]:
    """Scan a user message for WAL-worthy content.

    Returns list of WALEntry objects to write to SESSION-STATE.md.
    """
    entries: list[WALEntry] = []
    text = user_message.strip()

    if not text:
        return entries

    for pattern, category in WAL_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            # Extract relevant snippet (up to 200 chars around the match)
            start = max(0, match.start() - 50)
            end = min(len(text), match.end() + 150)
            snippet = text[start:end].strip()
            entries.append(WALEntry(category=category, detail=snippet))

    return entries


def _uid_tag(user_id: str) -> str:
    """Return a formatted user tag string, or empty string if no user_id."""
    return f" [user:{user_id}]" if user_id else ""


def wal_write(
    entries: list[WALEntry],
    workspace_dir: str = "/data/workspace",
    user_id: str = "",
) -> None:
    """Write WAL entries to shared SESSION-STATE.md and MEMORY.md.

    All entries go to workspace root (per-agent, shared across users).
    Entries are tagged with user_id so the bot knows who said what.
    """
    if not entries:
        return

    ws = Path(workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)
    state_path = ws / "SESSION-STATE.md"

    # Ensure file exists with header
    if not state_path.exists():
        state_path.write_text("# SESSION-STATE.md — Active Working Memory\n\n", encoding="utf-8")

    lines: list[str] = []
    uid_tag = _uid_tag(user_id)
    for entry in entries:
        lines.append(f"- [{entry.timestamp}]{uid_tag} **{entry.category}**: {entry.detail}\n")

    with open(state_path, "a", encoding="utf-8") as f:
        f.writelines(lines)

    logger.debug("WAL wrote %d entries to SESSION-STATE.md", len(entries))

    # Save durable facts to MEMORY.md (names, preferences, explicit "remember" requests)
    durable = [e for e in entries if e.category in DURABLE_CATEGORIES]
    if durable:
        _append_to_memory(durable, workspace_dir, user_id)

    # Notify hooks with combined content
    _notify_hooks("".join(lines), "SESSION-STATE.md")


def _append_to_memory(
    entries: list[WALEntry],
    workspace_dir: str,
    user_id: str = "",
) -> None:
    """Append durable facts to shared MEMORY.md, avoiding duplicates."""
    ws = Path(workspace_dir)
    memory_path = ws / "MEMORY.md"

    # Read existing content to check for duplicates
    existing = ""
    if memory_path.exists():
        existing = memory_path.read_text(encoding="utf-8")

    new_lines: list[str] = []
    existing_lines = [line.lower().strip() for line in existing.splitlines() if line.strip()]
    uid_tag = f" [user:{user_id}]" if user_id else ""
    for entry in entries:
        # Dedup: skip if a sufficiently similar line already exists
        detail_lower = entry.detail[:80].lower()
        if len(detail_lower) < 10:
            is_dup = any(
                detail_lower in eline and len(detail_lower) >= len(eline) * 0.3
                for eline in existing_lines
            )
        else:
            is_dup = any(detail_lower in eline for eline in existing_lines)
        if is_dup:
            logger.debug("Skipping duplicate memory: %s", entry.detail[:50])
            continue
        new_line = f"- **{entry.category}**:{uid_tag} {entry.detail}\n"
        new_lines.append(new_line)
        existing_lines.append(new_line.lower().strip())

    if not new_lines:
        return

    prefix_lines: list[str] = []
    if not existing.strip():
        prefix_lines.append("# MEMORY.md - Long-Term Memory\n\n")

    section_header = "## Auto-captured\n"
    if section_header not in existing:
        prefix_lines.append(f"\n{section_header}\n")

    with open(memory_path, "a", encoding="utf-8") as f:
        if prefix_lines:
            f.writelines(prefix_lines)
        f.writelines(new_lines)

    logger.info("Saved %d durable facts to MEMORY.md", len(new_lines))
    _notify_hooks("".join(new_lines), "MEMORY.md")


def write_daily_note(
    content: str,
    workspace_dir: str = "/data/workspace",
    user_id: str = "",
) -> None:
    """Append content to shared daily note (per-agent, not per-user).

    All users' conversation summaries go to the same daily file,
    tagged with user_id. This is the OpenClaw approach.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ws = Path(workspace_dir)
    memory_dir = ws / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    daily_path = memory_dir / f"{today}.md"

    if not daily_path.exists():
        daily_path.write_text(f"# Daily Notes — {today}\n\n", encoding="utf-8")

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    uid_tag = f" [user:{user_id}]" if user_id else ""
    with open(daily_path, "a", encoding="utf-8") as f:
        f.write(f"\n## [{ts}]{uid_tag}\n{content}\n")

    _notify_hooks(content, f"memory/{today}.md")


def memory_search(
    query: str,
    workspace_dir: str = "/data/workspace",
    user_id: str = "",
) -> list[dict]:
    """Search shared memory files for matching content.

    All memory is per-agent (shared). user_id parameter is kept
    for API compatibility but doesn't filter results — the bot
    sees everything and decides behaviorally what to share.
    """
    results: list[dict] = []
    ws = Path(workspace_dir)
    query_lower = query.lower()

    # Search shared MEMORY.md
    _search_file(ws / "MEMORY.md", "MEMORY.md", query_lower, results)

    # Search shared SESSION-STATE.md
    _search_file(ws / "SESSION-STATE.md", "SESSION-STATE.md", query_lower, results)

    # Search shared daily notes
    memory_dir = ws / "memory"
    if memory_dir.exists():
        for note in sorted(memory_dir.glob("*.md"), reverse=True)[:30]:
            _search_file(note, f"memory/{note.name}", query_lower, results)

    return results[:50]


def _search_file(
    path: Path,
    display_name: str,
    query_lower: str,
    results: list[dict],
) -> None:
    """Search a single file for query matches, appending to results."""
    if not path.exists():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if query_lower not in content.lower():
        return
    for line_no, line in enumerate(content.splitlines(), 1):
        if query_lower in line.lower():
            results.append({
                "file": display_name,
                "line": line_no,
                "content": line.strip(),
            })
