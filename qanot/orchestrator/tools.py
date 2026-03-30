"""Tool registrations for the orchestrator subsystem.

Registers 6 tools replacing 14 from delegate.py + subagent.py:
1. spawn_agent — unified spawn (sync/async/conversation)
2. list_agents — available agents + active runs
3. cancel_agent — cancel a running agent
4. view_board — read shared project board
5. clear_board — clear project board
6. agent_history — view past agent results
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from qanot.orchestrator.types import SpawnParams, MODE_SYNC, MODE_ASYNC, MODE_CONVERSATION
from qanot.orchestrator.announce import format_sync_result, build_announce_payload
from qanot.orchestrator.tool_policy import MAX_SPAWN_DEPTH

if TYPE_CHECKING:
    from qanot.config import Config
    from qanot.orchestrator.manager import SubagentManager
    from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)


def register_orchestrator_tools(
    registry: ToolRegistry,
    manager: SubagentManager,
    config: Config,
    depth: int = 0,
    *,
    get_user_id: callable = None,
    get_chat_id: callable = None,
    caller_agent_id: str = "",
) -> None:
    """Register orchestration tools on a tool registry.

    Args:
        registry: Tool registry to register on
        manager: SubagentManager instance
        config: Config instance
        depth: Current agent depth (0=main)
        get_user_id: Callable returning current user_id
        get_chat_id: Callable returning current chat_id
        caller_agent_id: ID of the calling agent (for access control)
    """
    # Don't register spawn tools at max depth (leaf agents)
    can_spawn = depth < MAX_SPAWN_DEPTH

    # Build dynamic agent enum
    available = manager.get_available_agents()
    agent_ids = sorted(available.keys())

    if can_spawn:
        _register_spawn_agent(registry, manager, config, depth, agent_ids, get_user_id, get_chat_id, caller_agent_id)
        _register_cancel_agent(registry, manager, get_user_id)

    _register_list_agents(registry, manager, config, depth, get_user_id)
    _register_view_board(registry, manager, get_user_id)
    _register_clear_board(registry, manager, get_user_id)
    _register_agent_history(registry, manager, get_user_id)


def _register_spawn_agent(
    registry: ToolRegistry,
    manager: SubagentManager,
    config: Config,
    depth: int,
    agent_ids: list[str],
    get_user_id: callable,
    get_chat_id: callable,
    caller_agent_id: str,
) -> None:
    """Register the spawn_agent tool."""

    async def handler(params: dict) -> str:
        task = params.get("task", "")
        agent_id = params.get("agent_id", "researcher")
        mode = params.get("mode", MODE_SYNC)
        context = params.get("context", "")
        max_turns = params.get("max_turns", 5)
        timeout = params.get("timeout", 120)

        # Validate mode
        if mode not in (MODE_SYNC, MODE_ASYNC, MODE_CONVERSATION):
            return json.dumps({"error": f"Invalid mode: {mode}. Use: sync, async, conversation"})

        user_id = (get_user_id() if get_user_id else None) or "default"
        chat_id = (get_chat_id() if get_chat_id else None)

        spawn_params = SpawnParams(
            task=task,
            agent_id=agent_id,
            mode=mode,
            context=context,
            timeout=timeout,
            max_turns=max_turns,
        )

        result = await manager.spawn(
            spawn_params,
            user_id=user_id,
            chat_id=chat_id,
            depth=depth,
            caller_agent_id=caller_agent_id or "main",
        )

        # If spawn returned an error string
        if isinstance(result, str):
            return json.dumps({"error": result})

        # Sync mode: result is already in the run
        if mode == MODE_SYNC:
            payload = build_announce_payload(result)
            return json.dumps(format_sync_result(payload))

        # Conversation mode: result is already in the run
        if mode == MODE_CONVERSATION:
            payload = build_announce_payload(result)
            return json.dumps(format_sync_result(payload))

        # Async mode: return confirmation
        return json.dumps({
            "status": "spawned",
            "run_id": result.run_id,
            "agent_id": result.agent_id,
            "agent_name": result.agent_name,
            "mode": "async",
            "note": "Result will be delivered automatically when complete. Do NOT poll.",
        })

    description = (
        "Spawn a sub-agent to handle a task. "
        "Modes: sync (wait for result), async (fire and deliver to chat), "
        "conversation (multi-turn dialogue with agent)."
    )

    # Build schema
    properties: dict = {
        "task": {
            "type": "string",
            "description": "What the agent should do. Be specific and clear.",
        },
        "agent_id": {
            "type": "string",
            "description": "Which agent to use.",
        },
        "mode": {
            "type": "string",
            "enum": [MODE_SYNC, MODE_ASYNC, MODE_CONVERSATION],
            "description": (
                "sync: wait for result (default). "
                "async: fire and deliver result to chat when done. "
                "conversation: multi-turn dialogue with agent."
            ),
            "default": MODE_SYNC,
        },
        "context": {
            "type": "string",
            "description": "Relevant context to pass to the agent (max 4000 chars).",
        },
        "max_turns": {
            "type": "integer",
            "description": "Max conversation turns (conversation mode only).",
            "default": 5,
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds.",
            "default": 120,
        },
    }

    # Add enum if we have agents
    if agent_ids:
        properties["agent_id"]["enum"] = agent_ids

    registry.register(
        name="spawn_agent",
        description=description,
        parameters={
            "type": "object",
            "properties": properties,
            "required": ["task"],
        },
        handler=handler,
        category="agent",
    )


def _register_cancel_agent(
    registry: ToolRegistry,
    manager: SubagentManager,
    get_user_id: callable,
) -> None:
    """Register the cancel_agent tool."""

    async def handler(params: dict) -> str:
        run_id = params.get("run_id", "").strip()
        if not run_id:
            # Cancel all for user
            user_id = (get_user_id() if get_user_id else None) or "default"
            count = await manager.cancel_all_for_user(user_id)
            return json.dumps({"cancelled": count, "scope": "all"})

        success = await manager.cancel(run_id)
        return json.dumps({"cancelled": success, "run_id": run_id})

    registry.register(
        name="cancel_agent",
        description="Cancel a running sub-agent by run_id, or cancel all active agents if no run_id provided.",
        parameters={
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run ID to cancel. Leave empty to cancel all.",
                },
            },
        },
        handler=handler,
        category="agent",
    )


def _register_list_agents(
    registry: ToolRegistry,
    manager: SubagentManager,
    config: Config,
    depth: int,
    get_user_id: callable,
) -> None:
    """Register the list_agents tool."""

    async def handler(params: dict) -> str:
        available = manager.get_available_agents()
        user_id = (get_user_id() if get_user_id else None) or "default"
        active_runs = manager.registry.get_active_for_user(user_id)

        agents_list = []
        for aid, info in available.items():
            entry = {
                "agent_id": aid,
                "name": info["name"],
                "description": info["prompt"][:120] + ("..." if len(info["prompt"]) > 120 else ""),
                "source": info.get("source", "builtin"),
                "has_identity_file": Path(config.workspace_dir, "agents", aid, "SOUL.md").exists(),
            }
            if info.get("model"):
                entry["model"] = info["model"]
            agents_list.append(entry)

        active_list = [
            {
                "run_id": r.run_id,
                "agent_id": r.agent_id,
                "agent_name": r.agent_name,
                "task": r.task[:100],
                "status": r.status,
                "mode": r.mode,
                "elapsed": f"{r.elapsed_seconds:.0f}s",
            }
            for r in active_runs
        ]

        return json.dumps({
            "agents": agents_list,
            "active_runs": active_list,
            "total_agents": len(agents_list),
            "active_count": len(active_list),
            "max_depth": MAX_SPAWN_DEPTH,
            "current_depth": depth,
            "can_spawn": depth < MAX_SPAWN_DEPTH,
        })

    registry.register(
        name="list_agents",
        description="List all available agents and currently active sub-agent runs.",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        category="agent",
    )


def _register_view_board(
    registry: ToolRegistry,
    manager: SubagentManager,
    get_user_id: callable,
) -> None:
    """Register the view_board tool."""

    async def handler(params: dict) -> str:
        user_id = (get_user_id() if get_user_id else None) or "default"
        board = manager.get_board(user_id)

        if not board:
            return json.dumps({"entries": [], "message": "Project board is empty."})

        agent_filter = params.get("agent_id", "").strip()
        entries = [
            {
                "agent_id": e.get("agent_id", ""),
                "agent_name": e.get("agent_name", ""),
                "result": e.get("result", "")[:1000],
                "status": e.get("status", ""),
                "elapsed": e.get("elapsed_seconds", 0),
                "tokens": e.get("tokens", 0),
                "timestamp": e.get("timestamp", 0),
            }
            for e in board
            if not agent_filter or e.get("agent_id") == agent_filter
        ]

        return json.dumps({"entries": entries, "total": len(entries)})

    registry.register(
        name="view_board",
        description="View the shared project board — see completed agent work.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Filter by agent ID (optional).",
                },
            },
        },
        handler=handler,
        category="agent",
    )


def _register_clear_board(
    registry: ToolRegistry,
    manager: SubagentManager,
    get_user_id: callable,
) -> None:
    """Register the clear_board tool."""

    async def handler(params: dict) -> str:
        user_id = (get_user_id() if get_user_id else None) or "default"
        count = len(manager.get_board(user_id))
        manager.clear_board(user_id)
        return json.dumps({"cleared": count})

    registry.register(
        name="clear_board",
        description="Clear the shared project board.",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        category="agent",
    )


def _register_agent_history(
    registry: ToolRegistry,
    manager: SubagentManager,
    get_user_id: callable,
) -> None:
    """Register the agent_history tool."""

    async def handler(params: dict) -> str:
        user_id = (get_user_id() if get_user_id else None) or "default"
        limit = min(params.get("limit", 10), 20)
        agent_filter = params.get("agent_id", "").strip()

        runs = manager.registry.get_recent_for_user(user_id, limit=limit * 2)

        if agent_filter:
            runs = [r for r in runs if r.agent_id == agent_filter]

        entries = [
            {
                "run_id": r.run_id,
                "agent_id": r.agent_id,
                "agent_name": r.agent_name,
                "task": r.task[:200],
                "status": r.status,
                "mode": r.mode,
                "result_preview": (r.result_text or "")[:500],
                "elapsed_seconds": round(r.elapsed_seconds, 1),
                "tokens": r.token_total,
                "created_at": r.created_at,
            }
            for r in runs[:limit]
        ]

        return json.dumps({"history": entries, "total": len(entries)})

    registry.register(
        name="agent_history",
        description="View past agent execution history and results.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Filter by agent ID (optional).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return (default 10, max 20).",
                    "default": 10,
                },
            },
        },
        handler=handler,
        category="agent",
    )
