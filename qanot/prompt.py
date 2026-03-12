"""Prompt builder — assembles system prompt from workspace files."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def build_system_prompt(
    workspace_dir: str = "/data/workspace",
    owner_name: str = "",
    bot_name: str = "",
    timezone_str: str = "Asia/Tashkent",
    context_percent: float = 0.0,
    total_tokens: int = 0,
    skill_path: str | None = None,
) -> str:
    """Build the full system prompt from workspace files.

    Concatenation order:
    1. SOUL.md (identity, principles)
    2. SKILL.md (proactive agent behaviors)
    3. TOOLS.md (tool configurations)
    4. AGENTS.md (operating rules)
    5. SESSION-STATE.md (active context)
    6. USER.md excerpt (human context)
    7. HEARTBEAT.md (if heartbeat trigger)
    8. SOUL_APPEND.md sections (plugin personality additions)
    """
    ws = Path(workspace_dir)
    parts: list[str] = []

    # 1. SOUL.md
    soul = _read_file(ws / "SOUL.md")
    if soul:
        parts.append(soul)

    # 2. SKILL.md (proactive agent skill)
    if skill_path:
        skill = _read_file(Path(skill_path))
        if skill:
            parts.append(skill)
    else:
        # Try default location
        skill = _read_file(ws / "SKILL.md")
        if skill:
            parts.append(skill)

    # 3. TOOLS.md
    tools = _read_file(ws / "TOOLS.md")
    if tools:
        parts.append(tools)

    # Check for plugin TOOLS files
    for p in sorted(ws.glob("*_TOOLS.md")):
        content = _read_file(p)
        if content:
            parts.append(content)

    # 4. AGENTS.md
    agents = _read_file(ws / "AGENTS.md")
    if agents:
        parts.append(agents)

    # 5. SESSION-STATE.md
    state = _read_file(ws / "SESSION-STATE.md")
    if state:
        parts.append(f"# Current Session State\n\n{state}")

    # 6. USER.md
    user = _read_file(ws / "USER.md")
    if user:
        parts.append(user)

    # 7. ONBOARDING.md — only include a short reminder, not the full file
    onboarding = _read_file(ws / "ONBOARDING.md")
    if onboarding and "state: complete" not in onboarding.lower():
        parts.append("# Onboarding\nUser onboarding is pending. Ask user about themselves naturally during conversation.")

    # Session info
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


def _read_file(path: Path) -> str:
    """Read a file if it exists, return empty string otherwise."""
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("Failed to read %s: %s", path, e)
    return ""
