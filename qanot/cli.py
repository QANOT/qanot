"""CLI entry point for Qanot AI."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


LOGO = r"""
  ___                    _
 / _ \  __ _ _ __   ___ | |_
| | | |/ _` | '_ \ / _ \| __|
| |_| | (_| | | | | (_) | |_
 \__\_\\__,_|_| |_|\___/ \__|

"""

CONFIG_TEMPLATE = {
    "bot_token": "",
    "provider": "openai",
    "model": "gpt-4.1",
    "api_key": "",
    "owner_name": "",
    "bot_name": "",
    "timezone": "Asia/Tashkent",
    "max_concurrent": 4,
    "max_context_tokens": 200000,
    "allowed_users": [],
    "workspace_dir": "/data/workspace",
    "sessions_dir": "/data/sessions",
    "cron_dir": "/data/cron",
    "plugins_dir": "/data/plugins",
    "plugins": [],
}


def cmd_init(args: list[str]) -> None:
    """Initialize a new Qanot project directory."""
    target = Path(args[0]) if args else Path.cwd()
    target.mkdir(parents=True, exist_ok=True)

    config_path = target / "config.json"
    if config_path.exists():
        print(f"config.json already exists in {target}")
        return

    # For local (non-Docker) use, set paths relative to target
    local_config = CONFIG_TEMPLATE.copy()
    local_config["workspace_dir"] = str(target / "workspace")
    local_config["sessions_dir"] = str(target / "sessions")
    local_config["cron_dir"] = str(target / "cron")
    local_config["plugins_dir"] = str(target / "plugins")

    config_path.write_text(json.dumps(local_config, indent=2, ensure_ascii=False))
    print(f"Created {config_path}")
    print()
    print("Next steps:")
    print(f"  1. Edit {config_path} — set bot_token and api_key")
    print(f"  2. Run: qanot start {target}")
    print()


def cmd_start(args: list[str]) -> None:
    """Start the Qanot agent."""
    # Determine config path
    if args:
        path = Path(args[0])
        if path.is_dir():
            config_path = path / "config.json"
        else:
            config_path = path
    else:
        # Check env, then current dir, then /data/config.json
        env_path = os.environ.get("QANOT_CONFIG")
        if env_path:
            config_path = Path(env_path)
        elif Path("config.json").exists():
            config_path = Path("config.json")
        else:
            config_path = Path("/data/config.json")

    if not config_path.exists():
        print(f"Config not found: {config_path}")
        print("Run 'qanot init' first, or set QANOT_CONFIG env var.")
        sys.exit(1)

    os.environ["QANOT_CONFIG"] = str(config_path.resolve())
    print(LOGO)
    print(f"Config: {config_path}")
    print()

    from qanot.main import main as run_main
    asyncio.run(run_main())


def cmd_version() -> None:
    from qanot import __version__
    print(f"qanot {__version__}")


def cmd_help() -> None:
    print(LOGO)
    print("Usage: qanot <command> [args]")
    print()
    print("Commands:")
    print("  init [dir]       Create config.json in directory (default: current)")
    print("  start [path]     Start agent (path to config.json or directory)")
    print("  version          Show version")
    print("  help             Show this help")
    print()
    print("Examples:")
    print("  qanot init mybot")
    print("  qanot start mybot")
    print("  qanot start /path/to/config.json")
    print("  QANOT_CONFIG=/data/config.json qanot start")
    print()


def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] == "help" or args[0] == "--help":
        cmd_help()
    elif args[0] == "init":
        cmd_init(args[1:])
    elif args[0] == "start":
        cmd_start(args[1:])
    elif args[0] == "version" or args[0] == "--version":
        cmd_version()
    else:
        # Default: treat as start
        cmd_start(args)


if __name__ == "__main__":
    main()
