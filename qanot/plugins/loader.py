"""Auto-discover and load plugins with manifest support."""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from qanot.plugins.base import Plugin, PluginManifest

if TYPE_CHECKING:
    from qanot.agent import ToolRegistry
    from qanot.config import Config

logger = logging.getLogger(__name__)

# Built-in plugins directory
BUILTIN_PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent / "plugins"


class PluginManager:
    """Manages plugin lifecycle — loading, registration, and shutdown."""

    def __init__(self):
        self._plugins: dict[str, Plugin] = {}
        self._manifests: dict[str, PluginManifest] = {}

    @property
    def loaded_plugins(self) -> dict[str, Plugin]:
        return dict(self._plugins)

    async def load_all(
        self, config: "Config", registry: "ToolRegistry",
    ) -> None:
        """Load all enabled plugins from config."""
        for plugin_cfg in config.plugins:
            if not plugin_cfg.enabled:
                logger.info("Plugin %s is disabled, skipping", plugin_cfg.name)
                continue

            name = plugin_cfg.name
            await self._load_one(name, plugin_cfg.config, config, registry)

    async def _load_one(
        self,
        name: str,
        plugin_config: dict,
        config: "Config",
        registry: "ToolRegistry",
    ) -> None:
        """Load a single plugin with error boundary."""
        logger.info("Loading plugin: %s", name)

        try:
            # Find plugin directory
            plugin_dir = _find_plugin_dir(name, config.plugins_dir)
            if plugin_dir is None:
                logger.error(
                    "Plugin '%s' not found. Searched: %s, %s",
                    name, BUILTIN_PLUGINS_DIR / name, Path(config.plugins_dir) / name,
                )
                return

            # Load manifest (plugin.json) if it exists
            manifest_path = plugin_dir / "plugin.json"
            if manifest_path.exists():
                manifest = PluginManifest.from_file(manifest_path)
                logger.info(
                    "Plugin %s v%s by %s",
                    manifest.name, manifest.version, manifest.author or "unknown",
                )
            else:
                manifest = PluginManifest.default(name)

            # Check plugin dependencies
            missing_deps = self._check_plugin_deps(manifest)
            if missing_deps:
                logger.error(
                    "Plugin '%s' requires plugins that aren't loaded: %s",
                    name, ", ".join(missing_deps),
                )
                return

            # Validate required config keys
            missing_config = _check_required_config(manifest, plugin_config)
            if missing_config:
                logger.warning(
                    "Plugin '%s' missing config keys: %s — may not work correctly",
                    name, ", ".join(missing_config),
                )

            # Load and instantiate
            plugin = await _load_from_path(plugin_dir, plugin_config)
            if plugin is None:
                return

            # Check for tool name conflicts
            tools = plugin.get_tools()
            conflicts = [t.name for t in tools if t.name in registry.tool_names]
            if conflicts:
                logger.warning(
                    "Plugin '%s' has tool name conflicts: %s — plugin tools will override",
                    name, ", ".join(conflicts),
                )

            # Register tools
            for t in tools:
                registry.register(
                    name=t.name,
                    description=t.description,
                    parameters=t.parameters,
                    handler=t.handler,
                )

            # Merge TOOLS.md and SOUL_APPEND into workspace
            _deploy_plugin_files(config.workspace_dir, plugin)

            # Track loaded plugin
            self._plugins[name] = plugin
            self._manifests[name] = manifest

            logger.info(
                "Plugin %s loaded: %d tools (%s)",
                name, len(tools), ", ".join(t.name for t in tools),
            )

        except Exception as e:
            logger.error("Failed to load plugin '%s': %s", name, e, exc_info=True)

    def _check_plugin_deps(self, manifest: PluginManifest) -> list[str]:
        """Check if required plugin dependencies are loaded."""
        missing = []
        for dep in manifest.plugin_deps:
            if dep not in self._plugins:
                missing.append(dep)
        return missing

    async def shutdown_all(self) -> None:
        """Call teardown() on all loaded plugins."""
        for name, plugin in self._plugins.items():
            try:
                await plugin.teardown()
                logger.info("Plugin %s shut down", name)
            except Exception as e:
                logger.error("Plugin %s teardown failed: %s", name, e)
        self._plugins.clear()
        self._manifests.clear()

    def get_plugin(self, name: str) -> Plugin | None:
        """Get a loaded plugin by name."""
        return self._plugins.get(name)

    def get_manifest(self, name: str) -> PluginManifest | None:
        """Get a plugin manifest by name."""
        return self._manifests.get(name)


# ── Module-level API (backward compatible) ────────────────

# Global plugin manager instance
_manager = PluginManager()


async def load_plugins(config: "Config", registry: "ToolRegistry") -> None:
    """Load plugins from config and register their tools."""
    await _manager.load_all(config, registry)


async def shutdown_plugins() -> None:
    """Shutdown all loaded plugins (call teardown)."""
    await _manager.shutdown_all()


def get_plugin_manager() -> PluginManager:
    """Get the global plugin manager."""
    return _manager


# ── Internal helpers ──────────────────────────────────────


def _find_plugin_dir(name: str, plugins_dir: str) -> Path | None:
    """Find plugin directory by name, searching builtin and external paths."""
    # Try built-in plugins first
    builtin_path = BUILTIN_PLUGINS_DIR / name
    if builtin_path.exists():
        return builtin_path

    # Try configured plugins directory
    external_path = Path(plugins_dir) / name
    if external_path.exists():
        return external_path

    return None


def _check_required_config(manifest: PluginManifest, config: dict) -> list[str]:
    """Check if all required config keys are present."""
    missing = []
    for key in manifest.required_config:
        if key not in config or not config[key]:
            missing.append(key)
    return missing


async def _load_from_path(plugin_dir: Path, config: dict) -> Plugin | None:
    """Load a plugin from a directory containing plugin.py."""
    plugin_file = plugin_dir / "plugin.py"
    if not plugin_file.exists():
        logger.error("No plugin.py found in %s", plugin_dir)
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
            logger.error("Failed to create import spec for %s", plugin_file)
            return None

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Find Plugin subclass
        plugin_cls = getattr(module, "QanotPlugin", None)
        if plugin_cls is None:
            # Try any Plugin subclass
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, Plugin) and attr is not Plugin:
                    plugin_cls = attr
                    break

        if plugin_cls is None:
            logger.error("No Plugin subclass found in %s", plugin_file)
            return None

        instance = plugin_cls()
        await instance.setup(config)
        return instance

    except Exception as e:
        logger.error("Plugin import failed for %s: %s", plugin_dir.name, e, exc_info=True)
        return None
    finally:
        if str_dir in sys.path:
            sys.path.remove(str_dir)


def _deploy_plugin_files(workspace_dir: str, plugin: Plugin) -> None:
    """Deploy plugin TOOLS.md and SOUL_APPEND to workspace."""
    ws = Path(workspace_dir)

    # Write plugin TOOLS to separate file (avoids polluting main TOOLS.md)
    if plugin.tools_md and plugin.name:
        plugin_tools_path = ws / f"{plugin.name}_TOOLS.md"
        plugin_tools_path.write_text(plugin.tools_md, encoding="utf-8")

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
