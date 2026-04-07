"""Tool registration for group orchestration.

Registers delegate_to_group when config.group_orchestration is enabled.
This is ADDITIONAL to the internal orchestrator tools (spawn_agent, etc.).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from qanot.config import Config
    from qanot.orchestrator.group import GroupOrchestrator
    from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)


def register_group_orchestration_tools(
    registry: ToolRegistry,
    config: Config,
    group_orchestrator: GroupOrchestrator,
    *,
    get_user_id: Callable[[], str | None] | None = None,
) -> None:
    """Register group orchestration tools on a tool registry.

    Only registers when group_orchestration is enabled and configured.
    """
    if not config.group_orchestration or not config.orchestration_group_id:
        return

    _register_delegate_to_group(
        registry, config, group_orchestrator, get_user_id,
    )


def _register_delegate_to_group(
    registry: ToolRegistry,
    config: Config,
    group_orchestrator: GroupOrchestrator,
    get_user_id: Callable[[], str | None] | None,
) -> None:
    """Register the delegate_to_group tool."""

    # Build agent enum from bots that have tokens (required for group presence)
    agent_ids = [
        ad.id for ad in config.agents if ad.bot_token
    ]

    async def handler(params: dict) -> str:
        agent_id = params.get("agent_id", "").strip()
        task = params.get("task", "").strip()
        wait = params.get("wait", False)

        if not agent_id:
            return json.dumps({"error": "agent_id is required"})
        if not task:
            return json.dumps({"error": "task is required"})

        # Validate agent has a bot_token (required for group presence)
        agent_def = None
        for ad in config.agents:
            if ad.id == agent_id:
                agent_def = ad
                break

        if not agent_def:
            return json.dumps({
                "error": f"Agent '{agent_id}' not found. Available: {agent_ids}",
            })
        if not agent_def.bot_token:
            return json.dumps({
                "error": (
                    f"Agent '{agent_id}' has no bot_token. "
                    "Group delegation requires each agent to have its own "
                    "Telegram bot token."
                ),
            })

        # Check agent bot is actually running
        if agent_id not in group_orchestrator.agent_bots:
            return json.dumps({
                "error": f"Agent '{agent_id}' bot is not running.",
            })

        try:
            if wait:
                timeout = config.bot_to_bot_chain_timeout
                result_text = await group_orchestrator.delegate_and_wait(
                    group_orchestrator.main_bot,
                    agent_id,
                    task,
                    timeout=timeout,
                )
                return json.dumps({
                    "status": "completed",
                    "agent_id": agent_id,
                    "result": result_text,
                })
            else:
                msg_id = await group_orchestrator.delegate(
                    group_orchestrator.main_bot,
                    agent_id,
                    task,
                )
                return json.dumps({
                    "status": "delegated",
                    "agent_id": agent_id,
                    "message_id": msg_id,
                    "message": (
                        f"Vazifa @{agent_id} agentga guruhda yuborildi. "
                        "Natija guruhda ko'rinadi."
                    ),
                })
        except ValueError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            logger.error("delegate_to_group failed: %s", e, exc_info=True)
            return json.dumps({"error": f"Delegation failed: {e}"})

    description = (
        "Delegate a task to a specialist agent bot in the Telegram group. "
        "The agent works visibly — the user can watch, interrupt, or redirect. "
        "Use wait=true when you need the result before continuing. "
        "Use wait=false (default) for fire-and-forget delegation."
    )

    properties: dict = {
        "agent_id": {
            "type": "string",
            "description": "Agent to delegate to (must have a bot_token).",
        },
        "task": {
            "type": "string",
            "description": "What to ask the agent to do.",
        },
        "wait": {
            "type": "boolean",
            "description": (
                "If true, block until agent responds (max chain timeout). "
                "If false, fire-and-forget."
            ),
            "default": False,
        },
    }

    if agent_ids:
        properties["agent_id"]["enum"] = agent_ids

    registry.register(
        name="delegate_to_group",
        description=description,
        parameters={
            "type": "object",
            "required": ["agent_id", "task"],
            "properties": properties,
        },
        handler=handler,
        category="agent",
    )
    logger.info("Registered delegate_to_group tool (%d agent bots)", len(agent_ids))
