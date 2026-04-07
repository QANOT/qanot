"""3-tier memory system with WAL protocol and working buffer.

Memory architecture (OpenClaw-style):
- All memory files are per-agent (shared at workspace root)
- MEMORY.md, SESSION-STATE.md, daily notes — all shared
- Entries tagged with user_id so bot knows who said what
- Conversation history isolation happens at session layer (agent._conversations)
- Privacy is behavioral — bot decides not to share based on context
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable

from qanot.utils import redact_secrets
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]  # Windows — file locking not available

logger = logging.getLogger(__name__)

# Per-workspace asyncio locks to prevent coroutine-level races on memory files
_ws_locks: dict[str, asyncio.Lock] = {}

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


# WAL trigger patterns (English + Uzbek) — compiled regex + category
WAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Corrections (EN + UZ)
    (re.compile(r"(?:it'?s|actually|no,?\s*i\s*meant|not\s+\w+,?\s+(?:but|it'?s))", re.IGNORECASE), "correction"),
    (re.compile(r"(?:yo'q|aslida|men\s+aytmoqchi|to'g'ri\s+emas)", re.IGNORECASE), "correction"),
    # Proper nouns (capitalized words after common intros)
    (re.compile(r"(?:my\s+name\s+is|i'?m|call\s+me|this\s+is)\s+([A-Z][a-z]+)", re.IGNORECASE), "proper_noun"),
    (re.compile(r"(?:mening?\s+ismim|men\s+)\s*([A-Z][a-z]+)", re.IGNORECASE), "proper_noun"),
    (re.compile(r"(?:sen(?:i|ing)?\s+(?:isming|nom))\s+(\w+)", re.IGNORECASE), "proper_noun"),
    # Preferences (EN + UZ)
    (re.compile(r"(?:i\s+(?:like|prefer|want|don'?t\s+like|hate|love))", re.IGNORECASE), "preference"),
    (re.compile(r"(?:men\s+(?:yoqtiraman|xohlayman|istardim|yomon\s+ko'raman))", re.IGNORECASE), "preference"),
    # Decisions (EN + UZ)
    (re.compile(r"(?:let'?s\s+(?:do|go|use|try)|go\s+with|use\s+)", re.IGNORECASE), "decision"),
    (re.compile(r"(?:qani|keling|ishlataylik|sinab\s+ko'raylik)", re.IGNORECASE), "decision"),
    # Specific values
    (re.compile(r"(?:\d{4}[-/]\d{2}[-/]\d{2}|https?://\S+|\b\d{5,}\b)", re.IGNORECASE), "specific_value"),
    # Remember commands (EN + UZ)
    (re.compile(r"(?:remember\s+(?:this|that)|don'?t\s+forget|eslab\s+qol|unutma|yodda\s+tut)", re.IGNORECASE), "remember"),
]

# Patterns that should also be saved to MEMORY.md (durable facts)
DURABLE_CATEGORIES: set[str] = {"proper_noun", "preference", "remember"}


def register_wal_pattern(pattern: str, category: str, durable: bool = False) -> None:
    """Register a custom WAL trigger pattern.

    Args:
        pattern: Regex pattern string (compiled with IGNORECASE).
        category: Category tag for matched entries (e.g. "preference").
        durable: If True, matches are also persisted to MEMORY.md.
    """
    compiled = re.compile(pattern, re.IGNORECASE)
    WAL_PATTERNS.append((compiled, category))
    if durable:
        DURABLE_CATEGORIES.add(category)


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
    text = user_message.strip()
    if not text:
        return []

    entries: list[WALEntry] = []
    for pattern, category in WAL_PATTERNS:
        match = pattern.search(text)
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

    # Cap file size: truncate oldest entries when over 100KB
    _MAX_STATE_SIZE = 100_000

    lines: list[str] = []
    uid_tag = _uid_tag(user_id)
    for entry in entries:
        detail = redact_secrets(entry.detail)
        lines.append(f"- [{entry.timestamp}]{uid_tag} **{entry.category}**: {detail}\n")

    # Atomic truncate-and-append under file lock to prevent concurrent data loss
    with open(state_path, "r+", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            content = f.read()
            truncated = False
            if len(content.encode("utf-8")) > _MAX_STATE_SIZE:
                header_end = content.find("\n\n")
                if header_end > 0:
                    header = content[:header_end + 2]
                    body = content[header_end + 2:]
                    keep = body[len(body) * 2 // 5:]  # drop oldest 40%
                    content = header + "[... older entries truncated ...]\n\n" + keep
                    truncated = True
                    logger.info("Truncated SESSION-STATE.md to %d bytes", len(content))
            if truncated:
                f.seek(0)
                f.write(content)
                f.truncate()
            # Append new entries
            f.seek(0, 2)  # seek to end
            f.writelines(lines)
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    logger.debug("WAL wrote %d entries to SESSION-STATE.md", len(entries))

    # Save durable facts to MEMORY.md (names, preferences, explicit "remember" requests)
    if durable := [e for e in entries if e.category in DURABLE_CATEGORIES]:
        _append_to_memory(durable, workspace_dir, user_id)

    # Notify hooks with combined content
    _notify_hooks("".join(lines), "SESSION-STATE.md")


def _append_to_memory(
    entries: list[WALEntry],
    workspace_dir: str,
    user_id: str = "",
) -> None:
    """Append durable facts to shared MEMORY.md, avoiding duplicates.

    Uses file locking to prevent concurrent writers from creating duplicates
    or corrupting the file.
    """
    ws = Path(workspace_dir)
    memory_path = ws / "MEMORY.md"

    # Ensure file exists
    if not memory_path.exists():
        memory_path.write_text("# MEMORY.md - Long-Term Memory\n\n", encoding="utf-8")

    # Read + dedup + append under file lock
    with open(memory_path, "r+", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            existing = f.read()

            new_lines: list[str] = []
            existing_lines = [line.lower().strip() for line in existing.splitlines() if line.strip()]
            uid_tag = _uid_tag(user_id)
            for entry in entries:
                detail_lower = entry.detail[:80].lower()
                is_dup = any(
                    detail_lower in eline
                    and (len(detail_lower) >= 10 or len(detail_lower) >= len(eline) * 0.3)
                    for eline in existing_lines
                )
                if is_dup:
                    logger.debug("Skipping duplicate memory: %s", entry.detail[:50])
                    continue
                new_line = f"- **{entry.category}**:{uid_tag} {redact_secrets(entry.detail)}\n"
                new_lines.append(new_line)
                existing_lines.append(new_line.lower().strip())

            if not new_lines:
                return

            prefix_lines: list[str] = []
            section_header = "## Auto-captured\n"
            if section_header not in existing:
                prefix_lines.append(f"\n{section_header}\n")

            f.seek(0, 2)  # seek to end
            if prefix_lines:
                f.writelines(prefix_lines)
            f.writelines(new_lines)
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

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
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    ws = Path(workspace_dir)
    memory_dir = ws / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    daily_path = memory_dir / f"{today}.md"

    if not daily_path.exists():
        daily_path.write_text(f"# Daily Notes — {today}\n\n", encoding="utf-8")

    ts = now.strftime("%H:%M:%S")
    uid_tag = _uid_tag(user_id)
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
