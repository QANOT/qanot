"""Auto-discover and load plugins."""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qanot.agent import ToolRegistry
    from qanot.config import Config

logger = logging.getLogger(__name__)

# Built-in plugins directory
BUILTIN_PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent / "plugins"


async def load_plugins(config: "Config", registry: "ToolRegistry") -> None:
    """Load plugins from config and register their tools."""
    for plugin_cfg in config.plugins:
        if not plugin_cfg.enabled:
            continue

        name = plugin_cfg.name
        logger.info("Loading plugin: %s", name)

        try:
            plugin = await _load_plugin(name, plugin_cfg.config)
            if plugin is None:
                logger.warning("Plugin %s not found", name)
                continue

            # Register tools
            tools = plugin.get_tools()
            for t in tools:
                registry.register(
                    name=t.name,
                    description=t.description,
                    parameters=t.parameters,
                    handler=t.handler,
                )

            # Merge TOOLS.md and SOUL_APPEND into workspace
            _deploy_plugin_files(config.workspace_dir, plugin)

            logger.info("Plugin %s loaded: %d tools", name, len(tools))
        except Exception as e:
            logger.error("Failed to load plugin %s: %s", name, e)


async def _load_plugin(name: str, config: dict):
    """Load a single plugin by name."""
    # Try built-in plugins first
    builtin_path = BUILTIN_PLUGINS_DIR / name
    if builtin_path.exists():
        return await _load_from_path(builtin_path, config)

    # Try external plugins directory
    external_path = Path("/data/plugins") / name
    if external_path.exists():
        return await _load_from_path(external_path, config)

    return None


async def _load_from_path(plugin_dir: Path, config: dict):
    """Load a plugin from a directory containing plugin.py."""
    plugin_file = plugin_dir / "plugin.py"
    if not plugin_file.exists():
        logger.warning("No plugin.py found in %s", plugin_dir)
        return None

    # Add plugin dir to path temporarily
    str_dir = str(plugin_dir)
    if str_dir not in sys.path:
        sys.path.insert(0, str_dir)

    try:
        # Dynamic import
        spec = importlib.util.spec_from_file_location(
            f"plugin_{plugin_dir.name}", str(plugin_file)
        )
        if spec is None or spec.loader is None:
            return None

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Find Plugin subclass
        plugin_cls = getattr(module, "QanotPlugin", None)
        if plugin_cls is None:
            # Try any Plugin subclass
            from qanot.plugins.base import Plugin
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, Plugin) and attr is not Plugin:
                    plugin_cls = attr
                    break

        if plugin_cls is None:
            logger.warning("No Plugin subclass found in %s", plugin_file)
            return None

        instance = plugin_cls()
        await instance.setup(config)
        return instance
    finally:
        if str_dir in sys.path:
            sys.path.remove(str_dir)


def _deploy_plugin_files(workspace_dir: str, plugin) -> None:
    """Deploy plugin TOOLS.md and SOUL_APPEND to workspace."""
    ws = Path(workspace_dir)

    # Append TOOLS.md content
    if plugin.tools_md:
        tools_path = ws / "TOOLS.md"
        if tools_path.exists():
            existing = tools_path.read_text(encoding="utf-8")
            if plugin.name not in existing:
                with open(tools_path, "a", encoding="utf-8") as f:
                    f.write(f"\n\n{plugin.tools_md}")
        else:
            tools_path.write_text(plugin.tools_md, encoding="utf-8")

    # Append SOUL_APPEND content
    if plugin.soul_append:
        soul_path = ws / "SOUL.md"
        if soul_path.exists():
            existing = soul_path.read_text(encoding="utf-8")
            # Check if already appended
            marker = plugin.soul_append.strip().splitlines()[0] if plugin.soul_append.strip() else ""
            if marker and marker not in existing:
                with open(soul_path, "a", encoding="utf-8") as f:
                    f.write(f"\n\n{plugin.soul_append}")
