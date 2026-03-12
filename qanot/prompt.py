"""Prompt builder — assembles system prompt from workspace files."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_FILE_CHARS = 20_000
MAX_TOTAL_CHARS = 150_000

_IDENTITY_LINE = "You are Qanot AI, a personal assistant."


def _truncate_content(content: str, max_chars: int) -> str:
    """Truncate content keeping 70% head and 20% tail with a marker."""
    from qanot.utils import truncate_with_marker
    return truncate_with_marker(content, max_chars)


def build_system_prompt(
    workspace_dir: str = "/data/workspace",
    owner_name: str = "",
    bot_name: str = "",
    timezone_str: str = "Asia/Tashkent",
    context_percent: float = 0.0,
    total_tokens: int = 0,
    skill_path: str | None = None,
    mode: str = "full",
) -> str:
    """Build the full system prompt from workspace files.

    Args:
        mode: Prompt assembly mode.
            ``"full"``    -- all sections (default).
            ``"minimal"`` -- SOUL.md + TOOLS.md + session info only.
            ``"none"``    -- identity line only.

    Concatenation order (full mode):
    1. SOUL.md (identity, principles)
    2. IDENTITY.md (agent name, vibe, emoji)
    3. SKILL.md (proactive agent behaviors)
    4. TOOLS.md (tool configurations)
    5. AGENTS.md (operating rules)
    6. SESSION-STATE.md (active context)
    7. USER.md excerpt (human context)
    8. BOOTSTRAP.md (first-run ritual, if exists)
    9. SOUL_APPEND.md sections (plugin personality additions)
    """
    if mode == "none":
        return _IDENTITY_LINE

    ws = Path(workspace_dir)
    parts: list[str] = []
    total_chars = 0

    def _add(content: str) -> None:
        """Append content to parts while tracking total char budget."""
        nonlocal total_chars
        if not content:
            return
        remaining = MAX_TOTAL_CHARS - total_chars
        if remaining <= 0:
            return
        content = _truncate_content(content, min(MAX_FILE_CHARS, remaining))
        parts.append(content)
        total_chars += len(content)

    # 1. SOUL.md
    _add(_read_file(ws / "SOUL.md"))

    if mode == "full":
        # 2. IDENTITY.md (agent name, vibe, emoji)
        _add(_read_file(ws / "IDENTITY.md"))

        # 3. SKILL.md (proactive agent skill)
        if skill_path:
            _add(_read_file(Path(skill_path)))
        else:
            _add(_read_file(ws / "SKILL.md"))

    # 4. TOOLS.md (included in both full and minimal)
    _add(_read_file(ws / "TOOLS.md"))

    if mode == "full":
        # Check for plugin TOOLS files
        for p in sorted(ws.glob("*_TOOLS.md")):
            _add(_read_file(p))

        # 5. AGENTS.md
        _add(_read_file(ws / "AGENTS.md"))

        # 6. SESSION-STATE.md
        state = _read_file(ws / "SESSION-STATE.md")
        if state:
            _add(f"# Current Session State\n\n{state}")

        # 7. USER.md
        _add(_read_file(ws / "USER.md"))

        # 8. BOOTSTRAP.md — first-run ritual (only if it exists)
        bootstrap = _read_file(ws / "BOOTSTRAP.md")
        if bootstrap:
            _add(bootstrap)

    # Hardcoded behavioral rules (not in templates — cannot be overwritten by agent)
    parts.append(
        "## Tool Call Style\n"
        "Default: do not narrate routine tool calls (just call the tool silently).\n"
        "Narrate only when it helps: multi-step work, complex problems, or when the user explicitly asks.\n"
        "Do not proactively mention internal file names (USER.md, IDENTITY.md, SOUL.md, MEMORY.md, SESSION-STATE.md, etc.).\n"
        "Do not say things like 'I am updating USER.md' or 'Let me save to IDENTITY.md' during routine operations.\n"
        "However, if the user explicitly asks to see or edit these files, comply — they are the owner.\n"
        "When a tool call fails, retry silently or work around it. Never show error details about file operations to the user.\n"
        "All internal bookkeeping is invisible unless the user specifically asks about it."
    )

    # Session info (included in both full and minimal)
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S UTC")

    session_info = (
        f"\n---\n\n"
        f"# Session Info\n"
        f"- **Date:** {date_str}\n"
        f"- **Time:** {time_str}\n"
        f"- **Timezone:** {timezone_str}\n"
        f"- **Context Usage:** {context_percent:.1f}%\n"
        f"- **Total Tokens:** {total_tokens:,}\n"
    )

    if context_percent >= 60:
        session_info += "- **WARNING:** Context above 60% — Working Buffer Protocol ACTIVE\n"

    parts.append(session_info)

    # Variable injection
    full = "\n\n---\n\n".join(parts)
    full = full.replace("{date}", date_str)
    full = full.replace("{bot_name}", bot_name)
    full = full.replace("{owner_name}", owner_name)
    full = full.replace("{timezone}", timezone_str)

    return full


def _read_file(path: Path, max_chars: int = MAX_FILE_CHARS) -> str:
    """Read a file if it exists, return empty string otherwise.

    Args:
        path: File path to read.
        max_chars: Maximum characters to keep. Content exceeding this
            limit is truncated with 70% head / 20% tail.
    """
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            return _truncate_content(content, max_chars)
    except Exception as e:
        logger.warning("Failed to read %s: %s", path, e)
    return ""
