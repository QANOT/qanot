"""Sub-agent spawning — isolated background agents for cron jobs and tasks."""

from __future__ import annotations

from qanot.config import Config
from qanot.context import ContextTracker
from qanot.providers.base import LLMProvider
from qanot.registry import ToolRegistry
from qanot.session import SessionWriter


async def spawn_isolated_agent(
    config: Config,
    provider: LLMProvider,
    tool_registry: ToolRegistry,
    prompt: str,
    session_id: str | None = None,
) -> str:
    """Spawn an isolated agent that runs independently.

    Used for cron jobs and background tasks.
    Returns the agent's final response.
    """
    # Local import to break circular dependency: this module is imported by
    # qanot.agent.__init__, but it needs the Agent class which lives in the
    # sibling .agent module.
    from .agent import Agent

    session = SessionWriter(config.sessions_dir)
    if session_id:
        session.new_session(session_id)

    context = ContextTracker(
        max_tokens=config.max_context_tokens,
        workspace_dir=config.workspace_dir,
    )

    agent = Agent(
        config=config,
        provider=provider,
        tool_registry=tool_registry,
        session=session,
        context=context,
        prompt_mode="minimal",
    )

    result = await agent.run_turn(prompt)
    return result
