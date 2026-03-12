"""3-tier memory system with WAL protocol and working buffer."""

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


def wal_write(entries: list[WALEntry], workspace_dir: str = "/data/workspace") -> None:
    """Write WAL entries to SESSION-STATE.md and durable facts to MEMORY.md."""
    if not entries:
        return

    state_path = Path(workspace_dir) / "SESSION-STATE.md"
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure file exists with header
    if not state_path.exists():
        state_path.write_text("# SESSION-STATE.md — Active Working Memory\n\n", encoding="utf-8")

    lines: list[str] = []
    for entry in entries:
        lines.append(f"- [{entry.timestamp}] **{entry.category}**: {entry.detail}\n")

    with open(state_path, "a", encoding="utf-8") as f:
        f.writelines(lines)

    logger.debug("WAL wrote %d entries to SESSION-STATE.md", len(entries))

    # Save durable facts to MEMORY.md (names, preferences, explicit "remember" requests)
    durable = [e for e in entries if e.category in DURABLE_CATEGORIES]
    if durable:
        _append_to_memory(durable, workspace_dir)

    # Notify hooks with combined content
    combined = "".join(lines)
    _notify_hooks(combined, "SESSION-STATE.md")


def _append_to_memory(entries: list[WALEntry], workspace_dir: str) -> None:
    """Append durable facts to MEMORY.md, avoiding duplicates."""
    memory_path = Path(workspace_dir) / "MEMORY.md"

    # Read existing content to check for duplicates
    existing = ""
    if memory_path.exists():
        existing = memory_path.read_text(encoding="utf-8")

    new_lines: list[str] = []
    for entry in entries:
        # Simple dedup: skip if the detail text is already in MEMORY.md
        if entry.detail[:80].lower() in existing.lower():
            logger.debug("Skipping duplicate memory: %s", entry.detail[:50])
            continue
        new_lines.append(f"- **{entry.category}**: {entry.detail}\n")

    if not new_lines:
        return

    # Ensure header exists
    if not existing.strip():
        existing = "# MEMORY.md - Long-Term Memory\n\n"

    # Append under "## Auto-captured" section
    section_header = "## Auto-captured\n"
    if section_header not in existing:
        existing = existing.rstrip() + f"\n\n{section_header}\n"

    with open(memory_path, "w", encoding="utf-8") as f:
        f.write(existing.rstrip() + "\n")
        f.writelines(new_lines)

    logger.info("Saved %d durable facts to MEMORY.md", len(new_lines))
    _notify_hooks("".join(new_lines), "MEMORY.md")


def write_daily_note(content: str, workspace_dir: str = "/data/workspace") -> None:
    """Append content to today's daily note."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    memory_dir = Path(workspace_dir) / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    daily_path = memory_dir / f"{today}.md"

    if not daily_path.exists():
        daily_path.write_text(f"# Daily Notes — {today}\n\n", encoding="utf-8")

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with open(daily_path, "a", encoding="utf-8") as f:
        f.write(f"\n## [{ts}]\n{content}\n")

    _notify_hooks(content, f"memory/{today}.md")


def memory_search(query: str, workspace_dir: str = "/data/workspace") -> list[dict]:
    """Search across daily notes and MEMORY.md for matching content."""
    results: list[dict] = []
    ws = Path(workspace_dir)
    query_lower = query.lower()

    # Search MEMORY.md
    memory_path = ws / "MEMORY.md"
    if memory_path.exists():
        content = memory_path.read_text(encoding="utf-8")
        if query_lower in content.lower():
            # Find matching sections
            for line_no, line in enumerate(content.splitlines(), 1):
                if query_lower in line.lower():
                    results.append({
                        "file": "MEMORY.md",
                        "line": line_no,
                        "content": line.strip(),
                    })

    # Search daily notes
    memory_dir = ws / "memory"
    if memory_dir.exists():
        for note in sorted(memory_dir.glob("*.md"), reverse=True)[:30]:
            content = note.read_text(encoding="utf-8")
            if query_lower in content.lower():
                for line_no, line in enumerate(content.splitlines(), 1):
                    if query_lower in line.lower():
                        results.append({
                            "file": f"memory/{note.name}",
                            "line": line_no,
                            "content": line.strip(),
                        })

    # Search SESSION-STATE.md
    state_path = ws / "SESSION-STATE.md"
    if state_path.exists():
        content = state_path.read_text(encoding="utf-8")
        if query_lower in content.lower():
            for line_no, line in enumerate(content.splitlines(), 1):
                if query_lower in line.lower():
                    results.append({
                        "file": "SESSION-STATE.md",
                        "line": line_no,
                        "content": line.strip(),
                    })

    return results[:50]  # Limit results
