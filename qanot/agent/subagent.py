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
    max_iterations: int = 50,
    origin_chat_id: int | None = None,
    origin_thread_id: int | None = None,
) -> str:
    """Spawn an isolated agent that runs independently.

    Used for cron jobs and background tasks. ``max_iterations`` is
    50 by default (vs. the interactive-turn default of 25): a typical
    cron prompt — "fetch the plan from Notion, derive today's topic,
    pick 10 words, write the reply to proactive-outbox.md" —
    realistically takes ~30 tool calls (memory_search + read_file +
    notion_query + execute_code + write_file). The 20:00 evening-quiz
    miss on 2026-05-23 was exactly this: agent ran 25 tool calls
    exploring and never reached write_file. 50 leaves headroom without
    making runaway loops cheap.

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
        max_iterations=max_iterations,
    )

    # Plumb the cron's origin onto the agent so `send_file` and
    # `tg_send_*` (which read agent.current_chat_id / current_thread_id
    # via getters from bootstrap/tool_registry.py) land in the
    # originating Telegram thread, not the base view. Without this the
    # outbox text routes correctly (payload carries the origin) but
    # any file the agent sends DIRECTLY misses the thread — the
    # 2026-05-24 13:00 deutsch-new-words anki .txt incident.
    if origin_chat_id is not None:
        agent._current_chat_id = origin_chat_id
    if origin_thread_id is not None:
        agent._current_thread_id = origin_thread_id

    result = await agent.run_turn(prompt)
    return result
