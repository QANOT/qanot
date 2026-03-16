"""Plugin management commands: new, list."""

from __future__ import annotations

import json
from pathlib import Path

from qanot.cli.utils import (
    LOGO,
    _bold,
    _cyan,
    _dim,
    _find_config,
    _green,
    _red,
    _yellow,
)


def cmd_plugin(args: list[str]) -> None:
    """Plugin management commands."""
    if not args:
        _plugin_help()
        return

    subcmd = args[0]
    if subcmd == "new":
        _plugin_new(args[1:])
    elif subcmd == "list":
        _plugin_list(args[1:])
    else:
        print(_red(f"Unknown plugin command: {subcmd}"))
        _plugin_help()


def _plugin_help() -> None:
    print(LOGO)
    print("Usage: qanot plugin <command>")
    print()
    print("Commands:")
    print("  new <name>         Scaffold a new plugin")
    print("  list [path]        List installed plugins")
    print()


def _plugin_new(args: list[str]) -> None:
    """Scaffold a new plugin directory with boilerplate."""
    if not args:
        print(_red("Usage: qanot plugin new <name>"))
        return

    name = args[0].lower().replace("-", "_").replace(" ", "_")

    # Determine target directory
    config_path = _find_config(args[1:])
    if config_path:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        plugins_dir = Path(raw.get("plugins_dir", config_path.parent / "plugins"))
    else:
        plugins_dir = Path.cwd() / "plugins"

    plugin_dir = plugins_dir / name

    if plugin_dir.exists():
        print(_red(f"Plugin directory already exists: {plugin_dir}"))
        return

    plugin_dir.mkdir(parents=True, exist_ok=True)

    # Generate plugin.json
    manifest = {
        "name": name,
        "version": "0.1.0",
        "description": f"{name} plugin for Qanot AI",
        "author": "",
        "dependencies": [],
        "required_config": [],
    }
    (plugin_dir / "plugin.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Generate plugin.py
    class_name = "".join(w.capitalize() for w in name.split("_"))
    plugin_py = f'''"""
{name} — Qanot AI plugin.

Usage:
  1. Add to config.json plugins array:
     {{"name": "{name}", "enabled": true, "config": {{}}}}
  2. Restart the bot.
"""

import json
from qanot.plugins.base import Plugin, ToolDef, tool


class QanotPlugin(Plugin):
    name = "{name}"
    description = "{name} plugin"
    version = "0.1.0"

    # Optional: markdown appended to the bot's TOOLS.md
    # tools_md = """## {class_name} Tools\\n- **{name}_hello** — ..."""

    # Optional: markdown appended to the bot's SOUL.md
    # soul_append = ""

    async def setup(self, config: dict) -> None:
        """Called once when the plugin loads. Use config for API keys etc."""
        self._config = config

    async def teardown(self) -> None:
        """Called on bot shutdown. Clean up connections here."""
        pass

    def get_tools(self) -> list[ToolDef]:
        return self._collect_tools()

    @tool(
        name="{name}_hello",
        description="Test tool — returns a greeting.",
        parameters={{
            "type": "object",
            "properties": {{
                "name": {{"type": "string", "description": "Name to greet"}},
            }},
        }},
    )
    async def hello(self, params: dict) -> str:
        who = params.get("name", "World")
        return json.dumps({{"message": f"Hello, {{who}}! from {name} plugin"}})
'''
    (plugin_dir / "plugin.py").write_text(plugin_py, encoding="utf-8")

    print(LOGO)
    print(_green(f"  Plugin scaffolded: {plugin_dir}"))
    print()
    print(f"  Files created:")
    print(f"    {_cyan('plugin.json')}  \u2014 manifest (name, version, deps)")
    print(f"    {_cyan('plugin.py')}    \u2014 plugin code with example tool")
    print()
    print(f"  Next steps:")
    print(f"    1. Edit {plugin_dir / 'plugin.py'} \u2014 add your tools")
    print(f"    2. Add to config.json:")
    config_snippet = '{"name": "' + name + '", "enabled": true}'
    print(f"       {_dim(config_snippet)}")
    print(f"    3. Restart the bot")
    print()


def _plugin_list(args: list[str]) -> None:
    """List installed plugins and their status."""
    config_path = _find_config(args)
    if not config_path:
        print(_red("No config.json found."))
        return

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    plugins_dir = Path(raw.get("plugins_dir", config_path.parent / "plugins"))
    configured = raw.get("plugins", [])

    print(LOGO)
    print(_bold("Installed Plugins"))
    print()

    if not configured:
        print(f"  {_dim('No plugins configured in config.json')}")
        print()

    # Show configured plugins
    for pl in configured:
        pname = pl if isinstance(pl, str) else pl.get("name", "?")
        enabled = pl.get("enabled", True) if isinstance(pl, dict) else True
        status = _green("enabled") if enabled else _dim("disabled")

        from qanot.plugins.loader import _find_plugin_dir
        plugin_dir = _find_plugin_dir(pname, str(plugins_dir))
        if plugin_dir:
            manifest_path = plugin_dir / "plugin.json"
            if manifest_path.exists():
                from qanot.plugins.base import PluginManifest
                m = PluginManifest.from_file(manifest_path)
                print(f"  {_cyan(pname)} v{m.version} [{status}]")
                if m.description:
                    print(f"    {_dim(m.description)}")
            else:
                print(f"  {_cyan(pname)} [{status}]")
            print(f"    {_dim(str(plugin_dir))}")
        else:
            print(f"  {_red(pname)} [{status}] \u2014 NOT FOUND")

    # Show discovered but unconfigured plugins
    if plugins_dir.exists():
        discovered = {d.name for d in plugins_dir.iterdir() if d.is_dir() and (d / "plugin.py").exists()}

        configured_names = {
            (pl if isinstance(pl, str) else pl.get("name", "?"))
            for pl in configured
        }
        unconfigured = discovered - configured_names
        if unconfigured:
            print()
            print(f"  {_yellow('Discovered but not configured:')}")
            for name in sorted(unconfigured):
                print(f"    {_dim(name + '/')}")

    print()
