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
from qanot.tools.builtin import register_builtin_tools
from qanot.tools.cron import register_cron_tools
from qanot.tools.doctor import register_doctor_tool
from qanot.tools.documents import register_document_tools
from qanot.tools.workspace import init_workspace
from qanot.plugins.loader import load_plugins, shutdown_plugins
from qanot.hooks import HookRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("qanot")


def _find_gemini_key(config) -> str | None:
    """Find a Gemini API key from config (multi-provider or dedicated field)."""
    # Check multi-provider configs
    for pc in config.providers:
        if pc.provider == "gemini" and pc.api_key:
            return pc.api_key
    # Check dedicated image_api_key
    if config.image_api_key:
        return config.image_api_key
    return None


def _anthropic_thinking_kwargs(provider_type: str, config) -> dict:
    """Return thinking keyword arguments for Anthropic providers; empty dict otherwise."""
    if provider_type == "anthropic":
        return {
            "thinking_level": config.thinking_level,
            "thinking_budget": config.thinking_budget,
            "code_execution": config.code_execution,
            "memory_tool": config.memory_tool,
            "context_editing": config.context_editing_enabled,
            "context_editing_trigger_tokens": config.context_editing_trigger_tokens,
            "context_editing_keep_tool_uses": config.context_editing_keep_tool_uses,
            "context_editing_clear_at_least_tokens": config.context_editing_clear_at_least_tokens,
        }
    return {}


def _create_provider(config):
    """Create LLM provider based on config.

    Supports two config formats:
    1. Single provider: { "provider": "anthropic", "model": "...", "api_key": "..." }
    2. Multi-provider: { "providers": [{ "name": "main", "provider": "anthropic", ... }, ...] }

    When multiple providers are configured, creates a FailoverProvider that
    automatically switches between them on errors.
    """
    from qanot.providers.failover import FailoverProvider, ProviderProfile, _create_single_provider

    # Multi-provider mode
    if config.providers:
        profiles = [
            ProviderProfile(
                name=pc.name,
                provider_type=pc.provider,
                api_key=pc.api_key,
                model=pc.model,
                base_url=pc.base_url or None,
                **_anthropic_thinking_kwargs(pc.provider, config),
            )
            for pc in config.providers
        ]
        provider = FailoverProvider(profiles)
        logger.info("Multi-provider mode: %s (failover enabled)", ", ".join(p.name for p in profiles))
        return provider

    # Single provider mode — reuse the same factory
    profile = ProviderProfile(
        name="default",
        provider_type=config.provider,
        api_key=config.api_key,
        model=config.model,
        **_anthropic_thinking_kwargs(config.provider, config),
    )
    return _create_single_provider(profile)


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

    # Create provider
    provider = _create_provider(config)
    logger.info("Provider initialized: %s", config.provider)

    # Wrap with routing provider if enabled (cost optimization)
    if config.routing_enabled:
        from qanot.routing import RoutingProvider
        routing_mid_model = getattr(config, "routing_mid_model", "claude-sonnet-4-6")
        provider = RoutingProvider(
            provider=provider,
            cheap_model=config.routing_model,
            mid_model=routing_mid_model,
            threshold=config.routing_threshold,
        )
        logger.info(
            "3-tier routing: simple → %s, moderate → %s, complex → %s",
            config.routing_model, routing_mid_model, config.model,
        )

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

    # Create tool registry
    tool_registry = ToolRegistry()

    # Create lifecycle hook registry
    agent_hooks = HookRegistry()

    # Initialize RAG engine
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
        # NOTE: the Anthropic memory_20250818 tool has its own hook (below);
        # this one covers qanot's built-in memory.py writes.
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

    # Register built-in tools
    # _agent_ref/_telegram_ref populated after creation; lambdas capture the lists
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

    register_builtin_tools(
        tool_registry, config.workspace_dir, context,
        rag_indexer=rag_indexer,
        get_user_id=lambda: _agent_ref[0].current_user_id if _agent_ref else "",
        get_cost_tracker=lambda: _agent_ref[0].cost_tracker if _agent_ref else None,
        exec_security=config.exec_security,
        exec_allowlist=config.exec_allowlist,
        approval_callback=_approval_callback if config.exec_security == "cautious" else None,
        get_bot=lambda: _telegram_ref[0].bot if _telegram_ref else None,
        get_chat_id=lambda: _agent_ref[0].current_chat_id if _agent_ref else None,
    )

    # Register Anthropic-compatible memory tool (/memories directory)
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

    # Register document generation tools (Word, Excel)
    register_document_tools(tool_registry, config.workspace_dir)

    # Register doctor diagnostics tool
    register_doctor_tool(tool_registry, config, context)

    # Register Uzbekistan business tools (currency, IKPU, payments, tax calculator)
    from qanot.tools.local import register_local_tools
    register_local_tools(tool_registry)

    # Connect MCP servers (Model Context Protocol)
    mcp_manager = None
    if config.mcp_servers:
        from qanot.mcp_client import MCPManager
        mcp_manager = MCPManager()
        mcp_count = await mcp_manager.connect_servers(config.mcp_servers)
        if mcp_count:
            tool_count = mcp_manager.register_tools(tool_registry)
            logger.info("MCP: %d servers connected, %d tools registered", mcp_count, tool_count)

    # Register browser control tools (Playwright)
    if config.browser_enabled:
        try:
            from qanot.tools.browser import register_browser_tools
            register_browser_tools(tool_registry, config.workspace_dir)
            logger.info("Browser control enabled (Playwright)")
        except Exception as e:
            logger.warning("Browser tools failed to register: %s", e)

    # Register web search tools (only if Brave API key is configured)
    if config.brave_api_key:
        from qanot.tools.web import register_web_tools
        register_web_tools(tool_registry, config.brave_api_key)
        logger.info("Web search enabled (Brave API)")

    # Find Gemini API key for image generation (registered after agent creation)
    gemini_api_key = _find_gemini_key(config)

    # Create session writer
    session = SessionWriter(config.sessions_dir)

    # Create scheduler (needs tool registry reference)
    scheduler = CronScheduler(
        config=config,
        provider=provider,
        tool_registry=tool_registry,
    )

    # Register cron tools (pass scheduler ref for reload notifications)
    register_cron_tools(tool_registry, config.cron_dir, scheduler_ref=scheduler)

    # Register skill management tools (create, list, run, delete)
    from qanot.tools.skill_tools import register_skill_tools
    register_skill_tools(
        tool_registry, config.workspace_dir,
        reload_callback=lambda: _agent_ref[0].load_skills(config.workspace_dir) if _agent_ref else None,
    )

    # Load plugins
    await load_plugins(config, tool_registry)

    # Register plugin lifecycle hooks with agent
    from qanot.plugins.loader import get_plugin_manager
    _pm = get_plugin_manager()
    if _pm:
        for plugin in _pm.loaded_plugins.values():
            agent_hooks.register_plugin(plugin)
        if agent_hooks.summary:
            logger.info("Plugin hooks registered: %s", agent_hooks.summary)

    # Freeze plugin registries to prevent runtime mutations from sub-agents
    from qanot.prompt import freeze_plugin_registries
    freeze_plugin_registries()

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

    get_user_id = lambda: agent.current_user_id

    # Register RAG tools and hooks (needs agent reference for get_user_id)
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

    # Register image generation tool (needs agent reference for pending images)
    if gemini_api_key:
        from qanot.tools.image import register_image_tools
        register_image_tools(
            tool_registry, gemini_api_key, config.workspace_dir,
            model=config.image_model,
            get_user_id=get_user_id,
        )
        logger.info("Image generation enabled (Nano Banana / %s)", config.image_model)

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

    # Register agent-initiated MCP management tools (mcp_test/propose/list/remove).
    # Register even when no servers are currently connected — the agent must be
    # able to propose the very first one. The tools themselves return a helpful
    # error if the `mcp` package is not installed.
    from qanot.tools.mcp_manage import register_mcp_tools
    register_mcp_tools(
        tool_registry,
        config,
        mcp_manager,
        telegram,
        get_user_id=lambda: agent.current_user_id or "",
        get_chat_id=lambda: agent.current_chat_id,
    )

    # Register config-secret management tools (delete_message, config_set_secret).
    # Agent-initiated: proposes change → user approves via Telegram button →
    # atomic write to /data/secrets.env + SecretRef in config.json → restart.
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

    # Register orchestrator tools (unified delegation + sub-agents)
    # Only when explicitly enabled — prevents model from over-delegating simple tasks
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

        # Register dynamic agent management tools (create/update/delete agents at runtime)
        from qanot.tools.agent_manager import register_agent_manager_tools
        register_agent_manager_tools(
            tool_registry, config, provider, tool_registry,
            get_user_id=get_user_id,
            subagent_manager=subagent_manager,
        )
        logger.info("Agent tools registered (orchestrator + management)")
    else:
        logger.info("Agent tools disabled (set agents_enabled: true to enable)")

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
            get_user_id=get_user_id,
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
            await voicecall_manager.start()
            telegram.voicecall_manager = voicecall_manager
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
        await shutdown_plugins()
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
