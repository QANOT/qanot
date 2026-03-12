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
from qanot.tools.builtin import register_builtin_tools
from qanot.tools.cron import register_cron_tools
from qanot.tools.workspace import init_workspace
from qanot.plugins.loader import load_plugins, shutdown_plugins

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("qanot")


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
        profiles = []
        for pc in config.providers:
            profiles.append(ProviderProfile(
                name=pc.name,
                provider_type=pc.provider,
                api_key=pc.api_key,
                model=pc.model,
                base_url=pc.base_url or None,
            ))
        provider = FailoverProvider(profiles)
        names = [p.name for p in profiles]
        logger.info("Multi-provider mode: %s (failover enabled)", ", ".join(names))
        return provider

    # Single provider mode — reuse the same factory
    profile = ProviderProfile(
        name="default",
        provider_type=config.provider,
        api_key=config.api_key,
        model=config.model,
    )
    return _create_single_provider(profile)


async def main() -> None:
    """Main entry point."""
    # Load config
    config = load_config()
    logger.info("Config loaded: provider=%s, model=%s", config.provider, config.model)

    # Initialize workspace (copy templates on first run)
    init_workspace(config.workspace_dir)

    # Create provider
    provider = _create_provider(config)
    logger.info("Provider initialized: %s", config.provider)

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
        from qanot.tools.rag import register_rag_tools

        embedder = create_embedder(config)
        if embedder:
            store = SqliteVecStore(
                db_path=f"{config.workspace_dir}/rag.db",
                dimensions=embedder.dimensions,
            )
            rag_engine = RAGEngine(embedder=embedder, store=store)
            rag_indexer = MemoryIndexer(rag_engine, config.workspace_dir)

            # Index existing memory files
            await rag_indexer.index_workspace()

            logger.info("RAG engine initialized with %s", type(embedder).__name__)

    # Register built-in tools
    register_builtin_tools(tool_registry, config.workspace_dir, context, rag_indexer=rag_indexer)

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

    # Register RAG tools and hooks (needs agent reference for get_user_id)
    if rag_engine is not None and rag_indexer is not None:
        from qanot.tools.rag import register_rag_tools
        from qanot.memory import add_write_hook

        agent.attach_rag(rag_indexer)

        register_rag_tools(
            tool_registry, rag_engine, config.workspace_dir,
            get_user_id=lambda: agent.current_user_id,
        )

        def _on_memory_write(content: str, source: str) -> None:
            asyncio.create_task(rag_indexer.index_text(content, source=source))

        add_write_hook(_on_memory_write)

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

    try:
        await telegram.start()
    finally:
        await shutdown_plugins()
        scheduler.stop()
        logger.info("Qanot AI shut down")


if __name__ == "__main__":
    asyncio.run(main())
