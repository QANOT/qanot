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

# WAL trigger patterns
WAL_PATTERNS = [
    # Corrections
    (r"(?:it'?s|actually|no,?\s*i\s*meant|not\s+\w+,?\s+(?:but|it'?s))", "correction"),
    # Proper nouns (capitalized words after common intros)
    (r"(?:my\s+name\s+is|i'?m|call\s+me|this\s+is)\s+([A-Z][a-z]+)", "proper_noun"),
    # Preferences
    (r"(?:i\s+(?:like|prefer|want|don'?t\s+like|hate|love))", "preference"),
    # Decisions
    (r"(?:let'?s\s+(?:do|go|use|try)|go\s+with|use\s+)", "decision"),
    # Specific values
    (r"(?:\d{4}[-/]\d{2}[-/]\d{2}|https?://\S+|\b\d{5,}\b)", "specific_value"),
]


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
    """Write WAL entries to SESSION-STATE.md."""
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

    # Notify hooks with combined content
    combined = "".join(lines)
    _notify_hooks(combined, "SESSION-STATE.md")


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
