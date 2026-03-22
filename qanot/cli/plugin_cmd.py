"""Plugin management commands: install, remove, search, info, new, list."""

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
    dispatch = {
        "install": _plugin_install,
        "remove": _plugin_remove,
        "uninstall": _plugin_remove,
        "search": _plugin_search,
        "info": _plugin_info,
        "scan": _plugin_scan,
        "verify": _plugin_verify,
        "new": _plugin_new,
        "list": _plugin_list,
        "ls": _plugin_list,
    }

    handler = dispatch.get(subcmd)
    if handler:
        handler(args[1:])
    else:
        print(_red(f"Unknown plugin command: {subcmd}"))
        _plugin_help()


def _plugin_help() -> None:
    print(LOGO)
    print("Usage: qanot plugin <command>")
    print()
    print("Commands:")
    print("  install <name|url>   Install from registry or git URL")
    print("  remove <name>        Remove an installed plugin")
    print("  search <keyword>     Search the plugin registry")
    print("  info <name>          Show plugin details")
    print("  list [path]          List all plugins (all tiers)")
    print("  scan <name>          Security scan a plugin")
    print("  verify               Verify all plugin integrity hashes")
    print("  new <name>           Scaffold a new plugin")
    print()
    print("Flags:")
    print("  --user               Install to ~/.qanot/plugins/ (user-level)")
    print("  --force              Skip security scan (NOT recommended)")
    print("  --registry=<url>     Use custom registry URL")
    print()
    print("Examples:")
    print("  qanot plugin install bito")
    print("  qanot plugin install https://github.com/user/qanot-plugin-crm")
    print("  qanot plugin install analytics --user")
    print("  qanot plugin remove analytics")
    print("  qanot plugin search crm")
    print()


# ── Install ────────────────────────────────────────────────


def _plugin_install(args: list[str]) -> None:
    """Install a plugin from registry or git URL."""
    if not args or args[0].startswith("--"):
        print(_red("Usage: qanot plugin install <name|git-url> [--user]"))
        return

    source = args[0]
    user_level = "--user" in args
    registry_url = _extract_flag(args, "--registry=")

    # Resolve plugins directory
    plugins_dir = _get_plugins_dir(args)

    print(LOGO)
    print(_bold("Plugin Install"))
    print()
    print(f"  Source: {_cyan(source)}")
    target_label = "~/.qanot/plugins/" if user_level else str(plugins_dir)
    print(f"  Target: {_dim(target_label)}")
    print()

    from qanot.plugins.registry import install_plugin, DEFAULT_REGISTRY_URL

    skip_security = "--force" in args
    if skip_security:
        print(f"  {_yellow('WARNING: --force skips security scan!')}")
        print()

    kwargs = {"registry_url": registry_url} if registry_url else {}
    ok, msg = install_plugin(
        source, plugins_dir, user_level=user_level,
        skip_security=skip_security, **kwargs,
    )

    if ok:
        print(f"  {_green('OK')} {msg}")
        print()
        print(f"  Next steps:")
        # Extract plugin name for config hint
        name = msg.split("'")[1] if "'" in msg else source
        print(f"    1. Add to config.json plugins array:")
        print(f"       {_dim(json.dumps({'name': name, 'enabled': True}))}")
        print(f"    2. Restart the bot")
    else:
        print(f"  {_red('FAILED')} {msg}")
    print()


# ── Remove ─────────────────────────────────────────────────


def _plugin_remove(args: list[str]) -> None:
    """Remove an installed plugin."""
    if not args:
        print(_red("Usage: qanot plugin remove <name>"))
        return

    name = args[0]
    plugins_dir = _get_plugins_dir(args)

    print(LOGO)
    print(_bold("Plugin Remove"))
    print()

    from qanot.plugins.registry import remove_plugin

    ok, msg = remove_plugin(name, plugins_dir)

    if ok:
        print(f"  {_green('OK')} {msg}")
        print()
        print(f"  Don't forget to remove from config.json plugins array.")
    else:
        print(f"  {_red('FAILED')} {msg}")
    print()


# ── Search ─────────────────────────────────────────────────


def _plugin_search(args: list[str]) -> None:
    """Search the plugin registry."""
    if not args:
        print(_red("Usage: qanot plugin search <keyword>"))
        return

    query = " ".join(a for a in args if not a.startswith("--"))
    registry_url = _extract_flag(args, "--registry=")

    print(LOGO)
    print(f"  Searching for: {_cyan(query)}")
    print()

    from qanot.plugins.registry import search_registry, DEFAULT_REGISTRY_URL

    url = registry_url or DEFAULT_REGISTRY_URL
    results = search_registry(query, url)

    if not results:
        print(f"  {_dim('No plugins found matching')} '{query}'")
        print(f"  {_dim('Registry:')} {url}")
        print()
        return

    for entry in results:
        tags = " ".join(f"#{t}" for t in entry.tags) if entry.tags else ""
        print(f"  {_cyan(entry.name)} v{entry.version}")
        if entry.description:
            print(f"    {entry.description}")
        if entry.author:
            print(f"    {_dim(f'by {entry.author}')}")
        if tags:
            print(f"    {_dim(tags)}")
        if entry.git_url:
            print(f"    {_dim(entry.git_url)}")
        print()

    print(f"  {len(results)} plugin(s) found")
    print(f"  Install: {_dim('qanot plugin install <name>')}")
    print()


# ── Info ───────────────────────────────────────────────────


def _plugin_info(args: list[str]) -> None:
    """Show detailed info about an installed plugin."""
    if not args:
        print(_red("Usage: qanot plugin info <name>"))
        return

    name = args[0]
    plugins_dir = _get_plugins_dir(args)

    print(LOGO)
    print(_bold(f"Plugin: {name}"))
    print()

    from qanot.plugins.registry import plugin_info

    info = plugin_info(name, plugins_dir)
    if not info:
        print(f"  {_red('Not found.')} Plugin '{name}' is not installed.")
        print()
        return

    # Display info
    _kv = lambda k, v: print(f"  {k:16s} {v}")
    _kv("Name:", _cyan(info.get("name", "?")))
    _kv("Version:", info.get("version", "?"))
    _kv("Tier:", info.get("tier", "?"))
    _kv("Path:", _dim(info.get("path", "?")))

    if info.get("description"):
        _kv("Description:", info["description"])
    if info.get("author"):
        _kv("Author:", info["author"])
    if info.get("source"):
        _kv("Source:", f"{info['source']} ({info.get('source_url', '?')})")
    if info.get("installed_at"):
        _kv("Installed:", info["installed_at"][:19])
    if info.get("tool_count"):
        _kv("Tools:", str(info["tool_count"]))
    if info.get("dependencies"):
        _kv("Dependencies:", ", ".join(info["dependencies"]))
    if info.get("required_config"):
        _kv("Config keys:", ", ".join(info["required_config"]))
    print()


# ── New (scaffold) ─────────────────────────────────────────


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
    print(f"    {_cyan('plugin.json')}  — manifest (name, version, deps)")
    print(f"    {_cyan('plugin.py')}    — plugin code with example tool")
    print()
    print(f"  Next steps:")
    print(f"    1. Edit {plugin_dir / 'plugin.py'} — add your tools")
    print(f"    2. Add to config.json:")
    config_snippet = '{"name": "' + name + '", "enabled": true}'
    print(f"       {_dim(config_snippet)}")
    print(f"    3. Restart the bot")
    print()


# ── List ───────────────────────────────────────────────────


def _plugin_list(args: list[str]) -> None:
    """List all plugins across all tiers."""
    plugins_dir = _get_plugins_dir(args)

    print(LOGO)
    print(_bold("Installed Plugins"))
    print()

    from qanot.plugins.registry import list_all_plugins

    plugins = list_all_plugins(str(plugins_dir))

    if not plugins:
        print(f"  {_dim('No plugins found.')}")
        print()
        return

    # Group by tier
    tiers = {"bundled": [], "user": [], "workspace": []}
    for p in plugins:
        tier = p.get("tier", "workspace")
        tiers.setdefault(tier, []).append(p)

    tier_labels = {
        "bundled": "Bundled (qanot package)",
        "user": "User (~/.qanot/plugins/)",
        "workspace": "Workspace (project plugins/)",
    }

    for tier, tier_plugins in tiers.items():
        if not tier_plugins:
            continue
        print(f"  {_bold(tier_labels.get(tier, tier))}")
        for p in tier_plugins:
            ver = p.get("version", "?")
            desc = p.get("description", "")
            print(f"    {_cyan(p['name'])} v{ver}")
            if desc:
                print(f"      {_dim(desc)}")
        print()

    # Show config status
    config_path = _find_config(args)
    if config_path:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            configured = {
                (pl if isinstance(pl, str) else pl.get("name", "?"))
                for pl in raw.get("plugins", [])
            }
            all_names = {p["name"] for p in plugins}
            unconfigured = all_names - configured
            if unconfigured:
                print(f"  {_yellow('Not in config.json:')}")
                for name in sorted(unconfigured):
                    print(f"    {_dim(name)}")
                print()
        except Exception:
            pass

    print(f"  Total: {len(plugins)} plugin(s)")
    print()


# ── Scan ───────────────────────────────────────────────────


def _plugin_scan(args: list[str]) -> None:
    """Security scan an installed plugin."""
    if not args:
        print(_red("Usage: qanot plugin scan <name>"))
        return

    name = args[0]
    plugins_dir = _get_plugins_dir(args)

    print(LOGO)
    print(_bold(f"Security Scan: {name}"))
    print()

    from qanot.plugins.registry import find_plugin_3tier
    from qanot.plugins.security import security_check

    plugin_path = find_plugin_3tier(name, str(plugins_dir))
    if not plugin_path:
        print(f"  {_red('Not found.')} Plugin '{name}' is not installed.")
        print()
        return

    is_safe, findings, summary = security_check(plugin_path, auto_block_critical=False)

    if not findings:
        print(f"  {_green('CLEAN')} — No security issues found")
        print()
        return

    # Show findings
    for f in findings:
        sev = f["severity"]
        if sev == "CRITICAL":
            icon = _red("CRITICAL")
        elif sev == "HIGH":
            icon = _red("HIGH")
        elif sev == "MEDIUM":
            icon = _yellow("MEDIUM")
        else:
            icon = _dim("LOW")

        loc = f.get("file", "?")
        if "line" in f:
            loc += f":{f['line']}"

        print(f"  [{icon}] {loc}")
        print(f"    {f['issue']}")
        if "code" in f:
            print(f"    {_dim(f['code'][:100])}")
        print()

    # Summary
    if is_safe:
        print(f"  {_yellow('REVIEW')} — {summary}")
    else:
        print(f"  {_red('UNSAFE')} — {summary}")
    print()


# ── Verify ─────────────────────────────────────────────────


def _plugin_verify(args: list[str]) -> None:
    """Verify integrity of all installed plugins against lock file hashes."""
    plugins_dir = _get_plugins_dir(args)

    print(LOGO)
    print(_bold("Plugin Integrity Verification"))
    print()

    from qanot.plugins.registry import read_lock, USER_PLUGINS_DIR
    from qanot.plugins.security import compute_plugin_hash, verify_plugin_hash

    checked = 0
    passed = 0
    failed = 0
    no_hash = 0

    for search_dir in [plugins_dir, USER_PLUGINS_DIR]:
        lock = read_lock(search_dir)
        for name, entry in lock.items():
            plugin_path = Path(entry.install_dir) if entry.install_dir else search_dir / name
            if not plugin_path.exists():
                print(f"  {_red('MISSING')} {name} — {plugin_path}")
                failed += 1
                checked += 1
                continue

            expected = getattr(entry, "sha256", "") or entry.to_dict().get("sha256", "")
            if not expected:
                print(f"  {_yellow('NO HASH')} {name}")
                no_hash += 1
                checked += 1
                continue

            if verify_plugin_hash(plugin_path, expected):
                print(f"  {_green('OK')} {name}")
                passed += 1
            else:
                actual = compute_plugin_hash(plugin_path)
                print(f"  {_red('TAMPERED')} {name}")
                print(f"    Expected: {_dim(expected[:16])}...")
                print(f"    Actual:   {_dim(actual[:16])}...")
                failed += 1
            checked += 1

    print()
    if checked == 0:
        print(f"  {_dim('No plugins with lock entries found.')}")
    else:
        print(f"  Checked: {checked}  Passed: {passed}  Failed: {failed}  No hash: {no_hash}")
    print()


# ── Helpers ────────────────────────────────────────────────


def _get_plugins_dir(args: list[str]) -> Path:
    """Resolve the workspace plugins directory from config or default."""
    remaining = [a for a in args if not a.startswith("--")]
    config_path = _find_config(remaining)
    if config_path:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            return Path(raw.get("plugins_dir", config_path.parent / "plugins"))
        except Exception:
            pass
    return Path.cwd() / "plugins"


def _extract_flag(args: list[str], prefix: str) -> str | None:
    """Extract a --flag=value from args."""
    for a in args:
        if a.startswith(prefix):
            return a[len(prefix) :]
    return None
