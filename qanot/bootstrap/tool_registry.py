"""All tool-registration wiring extracted from ``qanot.main``.

Tools are registered in two phases:

* :func:`register_pre_agent_tools` — runs before the :class:`~qanot.agent.Agent`
  is constructed. It uses the deferred-reference pattern (``_agent_ref`` /
  ``_telegram_ref`` lists) so tool handlers can resolve the agent and
  Telegram adapter once they exist.
* :func:`register_post_agent_tools` — runs after the agent is constructed.
  The few tools that genuinely need the live ``Agent`` object (RAG attach,
  orchestrator wiring) live here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from qanot.tools.builtin import register_builtin_tools
from qanot.tools.cron import register_cron_tools
from qanot.tools.doctor import register_doctor_tool
from qanot.tools.documents import register_document_tools

if TYPE_CHECKING:
    from qanot.agent import Agent
    from qanot.config import Config
    from qanot.context import ContextTracker
    from qanot.mcp_client import MCPManager
    from qanot.providers.base import LLMProvider
    from qanot.rag.engine import RAGEngine
    from qanot.rag.indexer import MemoryIndexer
    from qanot.registry import ToolRegistry
    from qanot.scheduler import CronScheduler
    from qanot.telegram import TelegramAdapter


async def register_pre_agent_tools(
    *,
    tool_registry: ToolRegistry,
    config: Config,
    context: ContextTracker,
    rag_indexer: MemoryIndexer | None,
    scheduler: CronScheduler,
    agent_ref: list,
    telegram_ref: list,
    approval_callback: Callable[[str, str, str], Awaitable[bool]] | None,
    logger: logging.Logger,
) -> MCPManager | None:
    """Register every tool that can be registered before the Agent exists.

    Returns the connected :class:`MCPManager` (or ``None`` when no MCP
    servers are configured) so :func:`register_post_agent_tools` and the
    main wiring can re-use it.
    """
    # Built-in tools (read/write/list/run_command/send_file/memory/session/cost)
    register_builtin_tools(
        tool_registry, config.workspace_dir, context,
        rag_indexer=rag_indexer,
        get_user_id=lambda: agent_ref[0].current_user_id if agent_ref else "",
        get_cost_tracker=lambda: agent_ref[0].cost_tracker if agent_ref else None,
        exec_security=config.exec_security,
        exec_allowlist=config.exec_allowlist,
        approval_callback=approval_callback,
        get_bot=lambda: telegram_ref[0].bot if telegram_ref else None,
        get_chat_id=lambda: agent_ref[0].current_chat_id if agent_ref else None,
    )

    # Anthropic-compatible memory tool (/memories directory)
    if config.memory_tool:
        from qanot.tools.memory_tool import register_memory_tool

        def _memory_write_hook(content: str, source: str) -> None:
            """Trigger RAG re-indexing when memory tool writes a file."""
            if rag_indexer:
                task = asyncio.create_task(rag_indexer.index_text(content, source=source))
                task.add_done_callback(
                    lambda t: logger.warning("Memory RAG index failed: %s", t.exception())
                    if not t.cancelled() and t.exception() else None
                )

        register_memory_tool(
            tool_registry, config.workspace_dir,
            on_write=_memory_write_hook if rag_indexer else None,
        )

    # Document generation tools (Word, Excel, PDF, PPTX) — 12 tools.
    # Gated: disable on bots that don't need them to stay under the tool-count
    # classifier threshold on OAuth paths.
    if config.document_tools_enabled:
        register_document_tools(tool_registry, config.workspace_dir)
    else:
        logger.info("Document tools disabled via document_tools_enabled=false")

    # Doctor diagnostics tool
    register_doctor_tool(tool_registry, config, context)

    # Uzbekistan business tools (currency, IKPU, payments, tax calc) — 6 tools.
    if config.local_business_tools_enabled:
        from qanot.tools.local import register_local_tools
        register_local_tools(tool_registry)
    else:
        logger.info("Local business tools disabled via local_business_tools_enabled=false")

    # MCP servers (Model Context Protocol)
    mcp_manager: MCPManager | None = None
    if config.mcp_servers:
        from qanot.mcp_client import MCPManager
        mcp_manager = MCPManager()
        mcp_count = await mcp_manager.connect_servers(config.mcp_servers)
        if mcp_count:
            tool_count = mcp_manager.register_tools(tool_registry)
            logger.info("MCP: %d servers connected, %d tools registered", mcp_count, tool_count)

    # Browser control (Playwright)
    if config.browser_enabled:
        try:
            from qanot.tools.browser import register_browser_tools
            register_browser_tools(tool_registry, config.workspace_dir)
            logger.info("Browser control enabled (Playwright)")
        except Exception as e:
            logger.warning("Browser tools failed to register: %s", e)

    # Web search (Brave) — only when API key is configured.
    # get_user_id resolves through agent_ref so per-user rate limiting
    # works once the agent is built.
    if config.brave_api_key:
        from qanot.tools.web import register_web_tools
        register_web_tools(
            tool_registry,
            config.brave_api_key,
            get_user_id=lambda: agent_ref[0].current_user_id if agent_ref else None,
            per_user_hourly=config.web_search_per_user_hourly,
        )
        logger.info("Web search enabled (Brave API)")

    # Cron tools (pass scheduler ref for reload notifications)
    register_cron_tools(tool_registry, config.cron_dir, scheduler_ref=scheduler)

    # Follow-up engine: stateful open-item tracker that uses the
    # scheduler under the hood. Disabling skips registration only; the
    # followups.json file in the workspace stays intact across toggles.
    if config.followup_enabled:
        from qanot.tools.followup import register_followup_tools
        register_followup_tools(
            tool_registry,
            workspace_dir=config.workspace_dir,
            cron_dir=config.cron_dir,
            timezone_name=config.timezone,
            scheduler_ref=scheduler,
        )
    else:
        logger.info("Follow-up tools disabled via followup_enabled=false")

    # Skill management tools (create, list, run, delete, install) — 5 tools.
    if config.skill_tools_enabled:
        from qanot.tools.skill_tools import register_skill_tools
        register_skill_tools(
            tool_registry, config.workspace_dir,
            reload_callback=lambda: agent_ref[0].load_skills(config.workspace_dir) if agent_ref else None,
        )
    else:
        logger.info("Skill tools disabled via skill_tools_enabled=false")

    return mcp_manager


def register_post_agent_tools(
    *,
    tool_registry: ToolRegistry,
    config: Config,
    agent: Agent,
    provider: LLMProvider,
    rag_engine: RAGEngine | None,
    rag_indexer: MemoryIndexer | None,
    gemini_api_key: str | None,
    mcp_manager: MCPManager | None,
    telegram: TelegramAdapter,
    logger: logging.Logger,
) -> Any:
    """Register every tool that needs the live :class:`Agent` instance.

    Returns the :class:`SubagentManager` when ``config.agents_enabled`` is
    true, otherwise ``None``. The manager is needed by the caller to wire
    Telegram /reset cancellation and group orchestration.
    """
    get_user_id = lambda: agent.current_user_id  # noqa: E731

    # RAG tools and memory write hook (needs agent reference for get_user_id)
    if rag_engine is not None and rag_indexer is not None:
        from qanot.tools.rag import register_rag_tools
        from qanot.memory import add_write_hook

        agent.attach_rag(rag_indexer)

        register_rag_tools(
            tool_registry, rag_engine, config.workspace_dir,
            get_user_id=get_user_id,
        )

        def _on_memory_write(content: str, source: str) -> None:
            task = asyncio.create_task(rag_indexer.index_text(content, source=source))
            def _on_done(t):
                if not t.cancelled() and (exc := t.exception()):
                    logger.warning("RAG index task failed: %s", exc)
            task.add_done_callback(_on_done)

        add_write_hook(_on_memory_write)

    # Image generation tool (needs agent reference for pending images)
    if gemini_api_key:
        from qanot.tools.image import register_image_tools
        register_image_tools(
            tool_registry, gemini_api_key, config.workspace_dir,
            model=config.image_model,
            get_user_id=get_user_id,
            per_user_hourly=config.image_gen_per_user_hourly,
        )
        logger.info("Image generation enabled (Nano Banana / %s)", config.image_model)

    # Video render tool (HyperFrames-based; talks to qanot-video service).
    # Only when video_engine is set to "hyperframes". "legacy_reels" leaves
    # the existing plugins/reels code path untouched; "off" registers nothing.
    if getattr(config, "video_engine", "off") == "hyperframes":
        from qanot.tools.video import register_video_tools
        register_video_tools(
            tool_registry,
            config=config,
            workspace_dir=config.workspace_dir,
            get_user_id=lambda: agent.current_user_id or None,
            get_chat_id=lambda: agent.current_chat_id,
            get_bot=lambda: telegram.bot,
        )

    # Agent-initiated MCP management tools (mcp_test/propose/list/remove) — 4 tools.
    if config.mcp_management_enabled:
        from qanot.tools.mcp_manage import register_mcp_tools
        register_mcp_tools(
            tool_registry,
            config,
            mcp_manager,
            telegram,
            get_user_id=lambda: agent.current_user_id or "",
            get_chat_id=lambda: agent.current_chat_id,
        )
    else:
        logger.info("MCP management tools disabled via mcp_management_enabled=false")

    # Config-secret management tools (delete_message, config_set_secret, config_toggle) — 3 tools.
    if config.config_management_enabled:
        from qanot.tools.config_manage import register_config_tools
        register_config_tools(
            tool_registry,
            config,
            telegram,
            get_user_id=lambda: agent.current_user_id or "",
            get_chat_id=lambda: agent.current_chat_id,
            get_message_id=lambda: agent.current_message_id,
            get_bot=lambda: telegram.bot,
        )
    else:
        logger.info("Config management tools disabled via config_management_enabled=false")

    # Orchestrator tools (unified delegation + sub-agents). Only when
    # explicitly enabled — prevents the model from over-delegating simple
    # tasks.
    subagent_manager = None
    if config.agents_enabled:
        from qanot.orchestrator import SubagentManager, register_orchestrator_tools
        subagent_manager = SubagentManager(
            config=config,
            provider=provider,
            parent_registry=tool_registry,
            announce_callback=telegram.send_message,
            persist_dir=config.workspace_dir,
        )
        register_orchestrator_tools(
            tool_registry, subagent_manager, config, depth=0,
            get_user_id=get_user_id,
            get_chat_id=lambda: agent.current_chat_id,
        )

        # Dynamic agent management tools (create/update/delete agents at runtime)
        from qanot.tools.agent_manager import register_agent_manager_tools
        register_agent_manager_tools(
            tool_registry, config, provider, tool_registry,
            get_user_id=get_user_id,
            subagent_manager=subagent_manager,
        )
        logger.info("Agent tools registered (orchestrator + management)")
    else:
        logger.info("Agent tools disabled (set agents_enabled: true to enable)")

    return subagent_manager
