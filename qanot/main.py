"""Entry point for Qanot AI agent."""

from __future__ import annotations

import asyncio
import logging
import sys

from qanot.config import load_config
from qanot.agent import Agent
from qanot.registry import ToolRegistry
from qanot.context import ContextTracker
from qanot.session import SessionWriter
from qanot.scheduler import CronScheduler
from qanot.telegram import TelegramAdapter
from qanot.backup import backup_workspace
from qanot.tools.workspace import init_workspace
from qanot.hooks import HookRegistry
from qanot.bootstrap import (
    build_provider,
    find_gemini_key,
    register_post_agent_tools,
    register_pre_agent_tools,
    setup_plugins,
    teardown_plugins,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("qanot")


def _get_container_memory_limit_bytes() -> int | None:
    """Return the container's effective memory limit in bytes, or None.

    Reads cgroup v2 (/sys/fs/cgroup/memory.max) first, then v1
    (/sys/fs/cgroup/memory/memory.limit_in_bytes). Returns None when
    we're not containerised or no limit is set ("max").
    """
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            raw = f.read().strip()
        if raw == "max":
            return None
        return int(raw)
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
            value = int(f.read().strip())
        # Kernel reports an absurdly large value when no limit is set.
        if value >= 1 << 62:
            return None
        return value
    except (OSError, ValueError):
        return None


async def main() -> None:
    """Main entry point."""
    # Load config
    config = load_config()
    logger.info("Config loaded: provider=%s, model=%s", config.provider, config.model)

    # Initialize workspace (copy templates on first run)
    init_workspace(config.workspace_dir)

    # Telemetry — per-call JSONL sink for real-traffic analysis. Best-effort;
    # never blocks or fails the agent. See qanot/telemetry.py for schema.
    from qanot import telemetry
    telemetry.init(config.workspace_dir)

    # Backup critical workspace files (non-fatal)
    if config.backup_enabled:
        try:
            backup_path = backup_workspace(config.workspace_dir)
            if backup_path:
                logger.info("Startup backup created: %s", backup_path)
        except Exception as e:
            logger.warning("Startup backup failed (non-fatal): %s", e)

    # Provider stack (single/multi/failover + optional routing wrapper)
    provider = build_provider(config, logger)

    # Create context tracker (auto-detect 1M for Opus/Sonnet 4.6)
    _1M_MODELS = ("claude-opus-4-6", "claude-sonnet-4-6")
    if config.max_context_tokens > 0:
        ctx_tokens = config.max_context_tokens
    elif any(m in config.model for m in _1M_MODELS):
        ctx_tokens = 1_000_000
    else:
        ctx_tokens = 200_000
    logger.info("Context window: %s tokens", f"{ctx_tokens:,}")

    context = ContextTracker(
        max_tokens=ctx_tokens,
        workspace_dir=config.workspace_dir,
    )

    # Tool registry + lifecycle hooks
    tool_registry = ToolRegistry()
    agent_hooks = HookRegistry()

    # Initialize RAG engine (FastEmbed CPU / Gemini / OpenAI)
    rag_engine = None
    rag_indexer = None
    if config.rag_enabled:
        from qanot.rag import create_embedder, SqliteVecStore, RAGEngine, MemoryIndexer

        embedder = create_embedder(config)
        dimensions = embedder.dimensions if embedder else 768
        store = SqliteVecStore(
            db_path=f"{config.workspace_dir}/rag.db",
            dimensions=dimensions,
        )
        rag_engine = RAGEngine(embedder=embedder, store=store)
        rag_indexer = MemoryIndexer(rag_engine, config.workspace_dir)

        # Index existing memory files
        await rag_indexer.index_workspace()

        # Wire real-time indexing: every WAL / daily-note / SESSION-STATE write
        # fires memory._notify_hooks(content, source). We re-ingest on each
        # write so rag_search returns fresh data without waiting for restart.
        # NOTE: the Anthropic memory_20250818 tool has its own hook (registered
        # later in register_pre_agent_tools); this one covers qanot's
        # built-in memory.py writes.
        from qanot.memory import add_write_hook

        def _rag_index_on_memory_write(content: str, source: str) -> None:
            if not rag_indexer:
                return
            task = asyncio.create_task(
                rag_indexer.index_text(content, source=source)
            )
            task.add_done_callback(
                lambda t: logger.warning("RAG index on memory write failed: %s", t.exception())
                if not t.cancelled() and t.exception() else None
            )

        add_write_hook(_rag_index_on_memory_write)

        if embedder:
            logger.info("RAG engine initialized with %s (hybrid: vector + %s)", type(embedder).__name__, rag_engine.fts_mode)
        else:
            logger.info("RAG engine initialized in FTS-only mode (no embedder available)")

    # Deferred references — populated after Agent / Telegram are constructed.
    # Tool handlers registered below capture these lists so they can resolve
    # the live agent and adapter at call time.
    _agent_ref: list = []
    _telegram_ref: list = []

    async def _approval_callback(user_id: str, command: str, reason: str) -> bool:
        """Route exec approval to Telegram inline buttons."""
        if not _telegram_ref or not _agent_ref:
            return False
        adapter = _telegram_ref[0]
        agent = _agent_ref[0]
        chat_id = agent.current_chat_id
        if not chat_id:
            return False
        return await adapter.request_approval(
            chat_id=chat_id,
            user_id=int(user_id) if user_id.isdecimal() else 0,
            command=command,
            reason=reason,
        )

    # Session writer + cron scheduler — both are needed by the pre-agent
    # tool registration (cron tools take a scheduler ref).
    session = SessionWriter(config.sessions_dir)
    scheduler = CronScheduler(
        config=config,
        provider=provider,
        tool_registry=tool_registry,
    )

    # Phase 1: register every tool that does NOT need the live Agent.
    mcp_manager = await register_pre_agent_tools(
        tool_registry=tool_registry,
        config=config,
        context=context,
        rag_indexer=rag_indexer,
        scheduler=scheduler,
        agent_ref=_agent_ref,
        telegram_ref=_telegram_ref,
        approval_callback=_approval_callback if config.exec_security == "cautious" else None,
        logger=logger,
    )

    # Find Gemini API key for image generation (registered post-agent)
    gemini_api_key = find_gemini_key(config)

    # Plugins: discover, register tools, wire lifecycle hooks, freeze registry.
    await setup_plugins(config, tool_registry, agent_hooks, logger)

    # Log registered tools
    logger.info("Tools registered: %s", ", ".join(tool_registry.tool_names))

    # Create agent
    agent = Agent(
        config=config,
        provider=provider,
        tool_registry=tool_registry,
        session=session,
        context=context,
        hooks=agent_hooks,
    )
    _agent_ref.append(agent)

    # Restore conversations from shutdown snapshot (if available)
    restored = agent.load_snapshot()
    if restored:
        logger.info("Restored %d conversations from previous session", restored)

    # Load skills from workspace
    agent.load_skills(config.workspace_dir)

    # Update scheduler with main agent
    scheduler.main_agent = agent

    # Start cron scheduler
    scheduler.start()

    # Create and start Telegram adapter
    telegram = TelegramAdapter(
        config=config,
        agent=agent,
        scheduler=scheduler,
    )
    _telegram_ref.append(telegram)

    # Expose MCP manager to Telegram adapter for /mcp command
    if mcp_manager:
        telegram._mcp_manager = mcp_manager

    # Phase 2: register tools that genuinely need the live Agent.
    subagent_manager = register_post_agent_tools(
        tool_registry=tool_registry,
        config=config,
        agent=agent,
        provider=provider,
        rag_engine=rag_engine,
        rag_indexer=rag_indexer,
        gemini_api_key=gemini_api_key,
        mcp_manager=mcp_manager,
        telegram=telegram,
        logger=logger,
    )

    # Wire sub-agent cancellation into Telegram /reset
    telegram.subagent_manager = subagent_manager

    # Start per-agent Telegram bots (each with their own bot_token)
    from qanot.agent_bot import start_agent_bots
    agent_bots = await start_agent_bots(config, provider, tool_registry, subagent_manager)

    # Wire group orchestration (visible multi-agent collaboration in a Telegram group)
    group_orchestrator = None
    if config.group_orchestration and config.orchestration_group_id:
        from qanot.orchestrator.loop_guard import LoopGuard
        from qanot.orchestrator.group import GroupOrchestrator
        from qanot.orchestrator.group_tools import register_group_orchestration_tools

        loop_guard = LoopGuard(
            max_depth=config.bot_to_bot_max_depth,
            cooldown_seconds=config.bot_to_bot_cooldown,
            chain_timeout_seconds=config.bot_to_bot_chain_timeout,
        )

        # Build agent_id -> AgentBot mapping from launched bots
        from qanot.tools.agent_manager import _active_agent_bots
        agent_bots_dict: dict[str, object] = dict(_active_agent_bots)
        # Also include bots from start_agent_bots that may not be in _active_agent_bots
        for ab in agent_bots:
            if ab.agent_def.id not in agent_bots_dict:
                agent_bots_dict[ab.agent_def.id] = ab

        group_orchestrator = GroupOrchestrator(
            config=config,
            main_bot=telegram.bot,
            agent_bots=agent_bots_dict,
            loop_guard=loop_guard,
            registry=subagent_manager.registry if subagent_manager else None,
        )

        # Inject into adapter
        telegram._group_orchestrator = group_orchestrator

        # Inject into each agent bot
        for ab in agent_bots:
            ab.group_orchestrator = group_orchestrator

        # Register delegate_to_group tool on the main bot's registry
        register_group_orchestration_tools(
            tool_registry, config, group_orchestrator,
            get_user_id=lambda: agent.current_user_id,
        )
        logger.info(
            "Group orchestration enabled (group_id=%d, %d agent bots)",
            config.orchestration_group_id, len(agent_bots_dict),
        )
    else:
        if config.group_orchestration and not config.orchestration_group_id:
            logger.warning(
                "group_orchestration is enabled but orchestration_group_id is 0 — "
                "group orchestration disabled"
            )

    # Start web dashboard (with optional webhook + webchat routes)
    dashboard = None
    if getattr(config, "dashboard_enabled", True):
        try:
            from qanot.dashboard import Dashboard
            dashboard = Dashboard(config, agent)

            # Register webhook/webchat routes BEFORE starting (router freezes on start)
            if config.webhook_enabled:
                from qanot.webhook import WebhookHandler
                webhook_handler = WebhookHandler(config, agent, scheduler)
                webhook_handler.register_routes(dashboard.app)

            if config.webchat_enabled:
                from qanot.webchat import WebChatAdapter
                webchat = WebChatAdapter(config, agent)
                webchat.register_routes(dashboard.app)

            await dashboard.start(port=getattr(config, "dashboard_port", 8765))
        except Exception as e:
            logger.warning("Dashboard failed to start: %s", e)

    # Start voice call manager (Pyrogram + py-tgcalls userbot)
    voicecall_manager = None
    if config.voicecall_enabled:
        # Voice call pipeline (torch + silero + faster-whisper + ntgcalls)
        # routinely peaks above 800MB of RSS. In a container with 256MB
        # limit (free tier) the Linux OOM killer trips mid-audio-load
        # and the process restart-loops, leaving the bot in a broken
        # half-joined state. Gate explicitly so operators see a clear
        # refusal rather than mysterious crashes.
        _VOICECALL_MIN_BYTES = 1_000_000_000  # ~1 GB
        try:
            limit = _get_container_memory_limit_bytes()
        except Exception:
            limit = None
        if limit is not None and limit < _VOICECALL_MIN_BYTES:
            logger.warning(
                "Voice call disabled — container memory limit %.0f MB is "
                "below the required 1 GB (upgrade plan to enable).",
                limit / 1_000_000,
            )
            config.voicecall_enabled = False
    if config.voicecall_enabled:
        try:
            from qanot.voicecall import VoiceCallManager
            voicecall_manager = VoiceCallManager(config=config, agent=agent)
            voicecall_manager.notify_owner = telegram.notify_admins
            await voicecall_manager.start()
            telegram.voicecall_manager = voicecall_manager
            if dashboard is not None:
                dashboard.voicecall_manager = voicecall_manager
            logger.info("Voice call manager started (py-tgcalls)")
        except ImportError:
            logger.warning(
                "Voice call dependencies not installed. "
                "Install with: pip install qanot[voicecall]"
            )
        except Exception as e:
            logger.warning("Voice call manager failed to start: %s", e)

    # Fire startup hooks
    await agent_hooks.fire("on_startup")

    try:
        await telegram.start()
    finally:
        # Stop voice call manager
        if voicecall_manager:
            try:
                await voicecall_manager.stop()
            except Exception as e:
                logger.warning("Error stopping voice call manager: %s", e)
        # Stop the shared MTProto client (used by voicecall + userbot).
        # Safe when nothing started it.
        try:
            from qanot.userbot_client import shutdown_userbot_client
            await shutdown_userbot_client()
        except Exception as e:
            logger.warning("Error stopping userbot client: %s", e)
        # Save conversation snapshots before shutdown
        try:
            saved = agent.save_snapshot()
            if saved:
                logger.info("Saved %d conversation snapshots for next startup", saved)
        except Exception as e:
            logger.warning("Failed to save conversation snapshots: %s", e)
        # Fire shutdown hooks
        await agent_hooks.fire("on_shutdown")
        # Cancel all running sub-agents gracefully
        if subagent_manager:
            try:
                await subagent_manager.shutdown()
            except Exception as e:
                logger.warning("Error shutting down sub-agents: %s", e)
        # Stop agent bots
        for ab in agent_bots:
            try:
                await ab.stop()
            except Exception as e:
                logger.warning("Error stopping agent bot '%s': %s", ab.agent_def.id, e)
        await teardown_plugins()
        scheduler.stop()
        # Close MCP server connections
        if mcp_manager:
            await mcp_manager.disconnect_all()
        # Close browser if it was used
        if config.browser_enabled:
            from qanot.tools.browser import _close_browser
            await _close_browser()
        # Close shared voice HTTP session
        from qanot.voice import close_voice_session
        await close_voice_session()
        logger.info("Qanot AI shut down")


if __name__ == "__main__":
    asyncio.run(main())
