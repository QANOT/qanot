"""Plugin discovery, lifecycle hook wiring, and teardown."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from qanot.plugins.loader import (
    get_plugin_manager,
    load_plugins,
    shutdown_plugins,
)

if TYPE_CHECKING:
    from qanot.config import Config
    from qanot.hooks import HookRegistry
    from qanot.registry import ToolRegistry


async def setup_plugins(
    config: Config,
    tool_registry: ToolRegistry,
    agent_hooks: HookRegistry,
    logger: logging.Logger,
) -> None:
    """Discover plugins, register their tools, and wire lifecycle hooks.

    After this call returns, the plugin registry is frozen so sub-agents
    cannot mutate it at runtime.
    """
    # Load plugins
    await load_plugins(config, tool_registry)

    # Register plugin lifecycle hooks with agent
    _pm = get_plugin_manager()
    if _pm:
        for plugin in _pm.loaded_plugins.values():
            agent_hooks.register_plugin(plugin)
        if agent_hooks.summary:
            logger.info("Plugin hooks registered: %s", agent_hooks.summary)

    # Freeze plugin registries to prevent runtime mutations from sub-agents
    from qanot.prompt import freeze_plugin_registries
    freeze_plugin_registries()


async def teardown_plugins() -> None:
    """Shutdown all loaded plugins (delegates to qanot.plugins.loader)."""
    await shutdown_plugins()
