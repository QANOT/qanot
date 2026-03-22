"""CLI entry point for Qanot AI."""

from __future__ import annotations

import json
import sys
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


def cmd_backup(args: list[str]) -> None:
    """Export workspace + sessions + cron to a timestamped .tar.gz archive."""
    import tarfile
    from datetime import datetime

    # Find config
    remaining = [a for a in args if not a.startswith("--")]
    config_path = _find_config(remaining)
    if not config_path:
        print(_red("\u2717 No config.json found."))
        print("  Run 'qanot init' first, or pass the path.")
        sys.exit(1)

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    project_dir = config_path.parent

    # Determine output path
    output_arg = None
    for a in args:
        if a.startswith("--output="):
            output_arg = a.split("=", 1)[1]
    if not output_arg:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bot_name = raw.get("bot_name", "qanot").replace(" ", "_").lower()
        output_arg = str(project_dir / f"{bot_name}_backup_{timestamp}.tar.gz")

    output_path = Path(output_arg)

    print(LOGO)
    print(_bold("Qanot Backup"))
    print()

    # Collect directories to back up
    backup_dirs_spec = [
        ("workspace_dir", "workspace"),
        ("sessions_dir", "sessions"),
        ("cron_dir", "cron"),
        ("plugins_dir", "plugins"),
    ]
    dirs_to_backup: list[tuple[Path, str]] = [
        (p, name)
        for config_key, name in backup_dirs_spec
        if (p := Path(raw.get(config_key, project_dir / name))).exists()
    ]

    # Always include config.json
    if not dirs_to_backup and not config_path.exists():
        print(_red("\u2717 Nothing to back up."))
        sys.exit(1)

    # Create archive
    file_count = 0
    with tarfile.open(output_path, "w:gz") as tar:
        # Add config
        tar.add(config_path, arcname="config.json")
        file_count += 1
        print(f"  {_green('+')} config.json")

        for dir_path, arcname_prefix in dirs_to_backup:
            count = 0
            for fpath in dir_path.rglob("*"):
                if fpath.is_file():
                    rel = fpath.relative_to(dir_path)
                    tar.add(fpath, arcname=f"{arcname_prefix}/{rel}")
                    count += 1
            file_count += count
            print(f"  {_green('+')} {arcname_prefix}/ ({count} files)")

    size_mb = output_path.stat().st_size / 1024 / 1024
    print()
    print(f"  {_green('\u2713')} Backup saved: {output_path}")
    print(f"  {_dim(f'{file_count} files, {size_mb:.1f} MB')}")
    print()


def cmd_update(args: list[str]) -> None:
    """Update Qanot to the latest version and restart."""
    import subprocess

    from qanot import __version__ as current

    print(LOGO)
    print(f"  Current version: {_cyan(current)}")
    print("  Checking for updates...", end=" ", flush=True)

    # Check latest version on PyPI
    try:
        import urllib.request
        with urllib.request.urlopen("https://pypi.org/pypi/qanot/json", timeout=10) as resp:
            data = json.loads(resp.read())
            latest = data.get("info", {}).get("version", "")
    except Exception:
        latest = ""

    if latest and latest == current:
        print(_green(f"\u2713 Already on latest ({current})"))
        return

    if latest:
        print(_yellow(f"\u2192 {latest} available"))
    else:
        print(_yellow("? Could not check (updating anyway)"))

    # Upgrade
    print("  Upgrading...", end=" ", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "qanot"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(_red("\u2717 Failed"))
        if result.stderr:
            print(f"  {_dim(result.stderr[:200])}")
        return

    # Verify new version
    check = subprocess.run(
        [sys.executable, "-c", "from qanot import __version__; print(__version__)"],
        capture_output=True, text=True,
    )
    new_version = check.stdout.strip() if check.returncode == 0 else "?"
    print(_green(f"\u2713 Updated to {new_version}"))

    # Restart if running
    if "--no-restart" not in args:
        config_path = _find_config([])
        if config_path:
            from qanot.daemon import daemon_status, daemon_restart
            is_running, _ = daemon_status(config_path)
            if is_running:
                print("  Restarting bot...", end=" ", flush=True)
                ok, msg = daemon_restart(config_path)
                if ok:
                    print(_green(f"\u2713 {msg}"))
                else:
                    print(_yellow(f"! {msg}"))
    print()


def cmd_version() -> None:
    from qanot import __version__
    print(f"qanot {__version__}")


def cmd_help() -> None:
    print(LOGO)
    print("Usage: qanot <command> [args]")
    print()
    print("Commands:")
    print("  init [dir]         Interactive setup wizard")
    print("  start [path]       Start bot (via OS service)")
    print("  stop [path]        Stop bot")
    print("  status [path]      Check if bot is running")
    print("  logs [path]        Tail bot logs")
    print("  restart [path]     Restart bot")
    print("  config <cmd>       Manage config (show/set/add-provider)")
    print("  doctor [path]      Health checks (--fix to auto-repair)")
    print("  backup [path]      Export workspace to .tar.gz")
    print("  plugin install     Install from registry or git URL")
    print("  plugin remove      Remove an installed plugin")
    print("  plugin search      Search the plugin registry")
    print("  plugin info        Show plugin details")
    print("  plugin list        List all plugins (all tiers)")
    print("  plugin new <name>  Scaffold a new plugin")
    print("  update             Update to latest version + restart")
    print("  version            Show version")
    print()
    print("Flags:")
    print("  start -f           Run in foreground (for Docker/debug)")
    print()
    print("Examples:")
    print("  qanot init         # Setup wizard")
    print("  qanot start        # Start bot")
    print("  qanot stop         # Stop bot")
    print("  qanot logs         # Watch logs")
    print()


def main() -> None:
    from qanot.cli.config_cmd import cmd_config
    from qanot.cli.daemon_cmd import cmd_logs, cmd_restart, cmd_start, cmd_status, cmd_stop
    from qanot.cli.doctor_cmd import cmd_doctor
    from qanot.cli.init_cmd import cmd_init
    from qanot.cli.plugin_cmd import cmd_plugin

    args = sys.argv[1:]

    # Commands that take remaining args
    _COMMANDS = {
        "init": cmd_init,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "logs": cmd_logs,
        "log": cmd_logs,
        "doctor": cmd_doctor,
        "backup": cmd_backup,
        "plugin": cmd_plugin,
        "config": cmd_config,
        "update": cmd_update,
    }
    # Commands with no args
    _NO_ARG_COMMANDS = {
        "help": cmd_help,
        "--help": cmd_help,
        "version": cmd_version,
        "--version": cmd_version,
    }

    if not args:
        cmd_help()
        return

    cmd = args[0]
    if cmd in _NO_ARG_COMMANDS:
        _NO_ARG_COMMANDS[cmd]()
    elif cmd in _COMMANDS:
        _COMMANDS[cmd](args[1:])
    else:
        # Default: treat as start
        cmd_start(args)


if __name__ == "__main__":
    main()
