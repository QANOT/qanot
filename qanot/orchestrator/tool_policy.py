"""Depth-based tool policy for sub-agents.

Replaces delegate.py's _build_delegate_registry() with a clean
cascading filter inspired by OpenClaw's subagent-capabilities.
"""

from __future__ import annotations

import logging

from qanot.orchestrator.types import ROLE_MAIN, ROLE_ORCHESTRATOR, ROLE_LEAF
from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Maximum nesting depth for sub-agent spawning
MAX_SPAWN_DEPTH = 3

# Tools ALWAYS denied for child agents (admin-only)
ALWAYS_DENIED = frozenset({
    "create_agent",
    "update_agent",
    "delete_agent",
    "restart_self",
})

# Tools only available to main/orchestrator agents (not leaf)
SPAWN_TOOLS = frozenset({
    "spawn_agent",
    "cancel_agent",
})


def resolve_role(depth: int) -> str:
    """Resolve agent role based on depth.

    depth 0 → main (full access)
    depth 1..MAX_SPAWN_DEPTH-1 → orchestrator (can spawn children)
    depth >= MAX_SPAWN_DEPTH → leaf (no spawning)
    """
    if depth == 0:
        return ROLE_MAIN
    if depth < MAX_SPAWN_DEPTH:
        return ROLE_ORCHESTRATOR
    return ROLE_LEAF


def build_child_registry(
    parent_registry: ToolRegistry,
    depth: int,
    tools_allow: list[str] | None = None,
    tools_deny: list[str] | None = None,
) -> ToolRegistry:
    """Build a filtered tool registry for a child agent.

    Cascading rules (OpenClaw-style):
    1. Start with all parent tools
    2. Remove ALWAYS_DENIED (admin tools)
    3. If leaf (depth >= MAX_SPAWN_DEPTH): remove SPAWN_TOOLS
    4. Apply tools_deny blacklist
    5. Apply tools_allow whitelist (if non-empty)
    """
    child = ToolRegistry()
    role = resolve_role(depth)

    deny_set = ALWAYS_DENIED.copy()
    if role == ROLE_LEAF:
        deny_set = deny_set | SPAWN_TOOLS
    if tools_deny:
        deny_set = deny_set | set(tools_deny)

    allow_set = set(tools_allow) if tools_allow else None

    for name in parent_registry.tool_names:
        # Skip denied tools
        if name in deny_set:
            continue

        # If allowlist is set, only include tools in it
        if allow_set and name not in allow_set:
            continue

        # Copy tool definition and handler
        tool_def = parent_registry._tools.get(name)
        handler = parent_registry._handlers.get(name)
        category = parent_registry._categories.get(name, "core")
        if tool_def and handler:
            child.register(
                name=name,
                description=tool_def["description"],
                parameters=tool_def["input_schema"],
                handler=handler,
                category=category,
            )

    logger.debug(
        "Built child registry: depth=%d role=%s tools=%d (parent=%d, denied=%d)",
        depth, role, len(child.tool_names), len(parent_registry.tool_names), len(deny_set),
    )
    return child
