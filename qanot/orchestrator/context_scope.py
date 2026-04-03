"""Context isolation and scoped prompt builder for sub-agents.

Replaces delegate.py's _build_agent_prompt() and _load_agent_identity()
with a clean, OpenClaw-inspired prompt template.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 4000


def load_agent_identity(workspace_dir: str, agent_id: str) -> str:
    """Load per-agent identity file if it exists.

    Looks for: {workspace_dir}/agents/{agent_id}/SOUL.md
    """
    soul_path = Path(workspace_dir) / "agents" / agent_id / "SOUL.md"
    try:
        if soul_path.exists():
            content = soul_path.read_text(encoding="utf-8").strip()
            if content:
                return content
    except Exception as e:
        logger.warning("Failed to read agent identity %s: %s", soul_path, e)
    return ""


def build_scoped_prompt(
    task: str,
    agent_identity: str,
    parent_context: str = "",
    board_summary: str = "",
    can_spawn: bool = False,
    agent_id: str = "",
) -> str:
    """Build system prompt for a child agent.

    Adapted from OpenClaw's buildSubagentSystemPrompt():
    - Clear role declaration
    - Explicit task
    - Stay focused, be ephemeral
    - Anti-polling (if can_spawn)
    """
    parts: list[str] = []

    # Identity (SOUL.md or builtin role prompt)
    if agent_identity:
        parts.append(agent_identity)
        parts.append("")

    # Task
    parts.append("## Your Task")
    parts.append(task)
    parts.append("")

    # Rules
    parts.append("## Rules")
    parts.append("1. Stay focused — do your assigned task, nothing else")
    parts.append("2. Complete the task — your final message will be reported to the parent agent")
    parts.append("3. Be concise but informative in your output")
    parts.append("4. Don't initiate side tasks or proactive actions")
    parts.append("5. Use tools directly — call web_search, read_file, etc. yourself")

    if can_spawn:
        parts.append("6. You CAN spawn sub-agents for parallel workstreams only")
        parts.append("7. Trust push-based completion — child results auto-announce; do not poll")
        parts.append("8. After spawning children, do NOT call list_agents or sleep. Wait for results.")
        parts.append("9. Track expected children and only send your final answer after ALL complete")
    else:
        parts.append("6. You are a leaf worker — do NOT spawn sub-agents. Execute directly with your tools.")

    # Context from parent
    if parent_context:
        ctx = parent_context[:MAX_CONTEXT_CHARS]
        parts.append("")
        parts.append("## Context from Parent")
        parts.append(ctx)

    # Board summary
    if board_summary:
        parts.append("")
        parts.append("## Project Board (other agents' completed work)")
        parts.append(board_summary)

    # Identity file note
    if agent_id:
        parts.append("")
        parts.append(f"Your identity file: agents/{agent_id}/SOUL.md (read/write to evolve)")

    return "\n".join(parts)


def fence_result(text: str) -> str:
    """Wrap child result in untrusted content fences (OpenClaw pattern)."""
    return (
        "<<<BEGIN_AGENT_RESULT>>>\n"
        f"{text}\n"
        "<<<END_AGENT_RESULT>>>"
    )


def get_board_summary(board: list[dict], exclude_agent: str = "", limit: int = 10) -> str:
    """Format project board entries for context injection."""
    if not board:
        return ""

    lines: list[str] = []
    for entry in board[-limit:]:
        if entry.get("agent_id") == exclude_agent:
            continue
        result = entry.get("result", "")
        preview = result[:500] + ("..." if len(result) > 500 else "")
        name = entry.get("agent_name", entry.get("agent_id", "?"))
        task = entry.get("task", entry.get("result", ""))[:100]
        lines.append(f"- **{name}**: {task}\n  Result: {preview}")

    return "\n".join(lines) if lines else ""
