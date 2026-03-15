"""Entry point for Qanot AI agent."""

from __future__ import annotations

import asyncio
import logging
import sys

from qanot.config import load_config
from qanot.agent import Agent, ToolRegistry
from qanot.context import ContextTracker
from qanot.session import SessionWriter
from qanot.scheduler import CronScheduler
from qanot.telegram import TelegramAdapter
from qanot.backup import backup_workspace
from qanot.tools.builtin import register_builtin_tools
from qanot.tools.cron import register_cron_tools
from qanot.tools.doctor import register_doctor_tool
from qanot.tools.workspace import init_workspace
from qanot.plugins.loader import load_plugins, shutdown_plugins

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
        return {"thinking_level": config.thinking_level, "thinking_budget": config.thinking_budget}
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


async def main() -> None:
    """Main entry point."""
    # Load config
    config = load_config()
    logger.info("Config loaded: provider=%s, model=%s", config.provider, config.model)

    # Initialize workspace (copy templates on first run)
    init_workspace(config.workspace_dir)

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

    # Create context tracker
    context = ContextTracker(
        max_tokens=config.max_context_tokens,
        workspace_dir=config.workspace_dir,
    )

    # Create tool registry
    tool_registry = ToolRegistry()

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
    )

    # Register doctor diagnostics tool
    register_doctor_tool(tool_registry, config, context)

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

    # Load plugins
    await load_plugins(config, tool_registry)

    # Log registered tools
    logger.info("Tools registered: %s", ", ".join(tool_registry.tool_names))

    # Create agent
    agent = Agent(
        config=config,
        provider=provider,
        tool_registry=tool_registry,
        session=session,
        context=context,
    )

    _agent_ref.append(agent)

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
            asyncio.create_task(rag_indexer.index_text(content, source=source))

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

    # Register sub-agent tools (needs agent + telegram for delivery)
    from qanot.tools.subagent import register_sub_agent_tools
    register_sub_agent_tools(
        tool_registry, config, provider, tool_registry,
        get_user_id=get_user_id,
        get_chat_id=lambda: agent.current_chat_id,
        send_callback=telegram.send_message,
    )

    # Register agent-to-agent delegation tools
    from qanot.tools.delegate import register_delegate_tools, set_notify_callback
    register_delegate_tools(
        tool_registry, config, provider, tool_registry,
        get_user_id=get_user_id,
    )

    # Register dynamic agent management tools (create/update/delete agents at runtime)
    from qanot.tools.agent_manager import register_agent_manager_tools
    register_agent_manager_tools(
        tool_registry, config, provider, tool_registry,
        get_user_id=get_user_id,
    )
    logger.info("Agent tools registered (delegation + management + sub-agent)")

    # Wire live agent monitoring notifications to Telegram
    async def _notify_user(text: str) -> None:
        try:
            if cid := agent.current_chat_id:
                await telegram.send_message(cid, text)
        except Exception:
            pass  # Non-fatal

    for uid in config.allowed_users:
        set_notify_callback(str(uid), _notify_user)

    # Start per-agent Telegram bots (each with their own bot_token)
    from qanot.agent_bot import start_agent_bots
    agent_bots = await start_agent_bots(config, provider, tool_registry)

    # Start web dashboard
    if getattr(config, "dashboard_enabled", True):
        try:
            from qanot.dashboard import Dashboard
            dashboard = Dashboard(config, agent)
            await dashboard.start(port=getattr(config, "dashboard_port", 8765))
        except Exception as e:
            logger.warning("Dashboard failed to start: %s", e)

    try:
        await telegram.start()
    finally:
        # Stop agent bots
        for ab in agent_bots:
            try:
                await ab.stop()
            except Exception as e:
                logger.warning("Error stopping agent bot '%s': %s", ab.agent_def.id, e)
        await shutdown_plugins()
        scheduler.stop()
        logger.info("Qanot AI shut down")


if __name__ == "__main__":
    asyncio.run(main())
