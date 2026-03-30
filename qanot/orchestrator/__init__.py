"""Orchestrator — unified multi-agent management for Qanot AI.

Replaces tools/delegate.py + tools/subagent.py with a clean,
OpenClaw-inspired architecture using push-based result delivery,
depth-based tool policies, and persistent run tracking.
"""

from qanot.orchestrator.types import SubagentRun, SpawnParams, AnnouncePayload
from qanot.orchestrator.registry import SubagentRegistry
from qanot.orchestrator.manager import SubagentManager
from qanot.orchestrator.tools import register_orchestrator_tools

__all__ = [
    "SubagentRun",
    "SpawnParams",
    "AnnouncePayload",
    "SubagentRegistry",
    "SubagentManager",
    "register_orchestrator_tools",
]
