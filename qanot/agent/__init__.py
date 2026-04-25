"""Core agent loop — the heart of Qanot AI."""

from __future__ import annotations

from qanot.registry import ToolRegistry  # re-export for compat

from .agent import Agent
from .loop import (
    BASE_DELAY,
    CONVERSATION_TTL,
    LONG_TOOL_TIMEOUT,
    MAX_COMPACTION_RETRIES,
    MAX_DELAY,
    MAX_ITERATIONS,
    TOOL_TIMEOUT,
    _CONTINUE,
    _FATAL,
    _LONG_RUNNING_TOOLS,
)
from .subagent import spawn_isolated_agent

__all__ = [
    "Agent",
    "ToolRegistry",
    "spawn_isolated_agent",
    "MAX_ITERATIONS",
    "TOOL_TIMEOUT",
    "LONG_TOOL_TIMEOUT",
    "CONVERSATION_TTL",
    "MAX_COMPACTION_RETRIES",
    "BASE_DELAY",
    "MAX_DELAY",
]
