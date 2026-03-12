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
from qanot.plugins.loader import load_plugins

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("qanot")


def _create_provider(config):
    """Create LLM provider based on config."""
    if config.provider == "anthropic":
        from qanot.providers.anthropic import AnthropicProvider
        return AnthropicProvider(api_key=config.api_key, model=config.model)
    elif config.provider == "openai":
        from qanot.providers.openai import OpenAIProvider
        return OpenAIProvider(api_key=config.api_key, model=config.model)
    else:
        raise ValueError(f"Unknown provider: {config.provider}")


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

    # Register built-in tools
    register_builtin_tools(tool_registry, config.workspace_dir, context)

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

    await telegram.start()


if __name__ == "__main__":
    asyncio.run(main())
