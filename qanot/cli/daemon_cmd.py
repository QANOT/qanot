"""Daemon/service management commands: start, stop, restart, status, logs."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from qanot.cli.utils import (
    LOGO,
    _cyan,
    _dim,
    _green,
    _red,
    _resolve_config,
)


def _pid_file(config_path: Path) -> Path:
    """Return PID file path for a given config."""
    return config_path.parent / ".qanot.pid"


def _log_file(config_path: Path) -> Path:
    """Return log file path for a given config."""
    return config_path.parent / "qanot.log"


def _is_running(pid_path: Path) -> tuple[bool, int]:
    """Check if bot is running. Returns (running, pid)."""
    if not pid_path.exists():
        return False, 0
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        return True, pid
    except (ValueError, ProcessLookupError, PermissionError):
        pid_path.unlink(missing_ok=True)
        return False, 0


def _start_subprocess(config_path: Path) -> None:
    """Fallback: start bot via subprocess (when OS service unavailable)."""
    import subprocess

    pid_path = _pid_file(config_path)
    log_path = _log_file(config_path)

    env = os.environ.copy()
    env["QANOT_CONFIG"] = str(config_path)
    env["PYTHONUNBUFFERED"] = "1"

    with open(log_path, "a") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "qanot"],
            env=env,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )

    pid_path.write_text(str(proc.pid))
    print(f"  {_green('\u2713')} Bot started via subprocess (PID {proc.pid})")


def cmd_start(args: list[str]) -> None:
    """Start the bot via OS service manager, or foreground with -f."""
    from qanot.daemon import daemon_install, daemon_start, daemon_status

    foreground = "--foreground" in args or "-f" in args
    config_path = _resolve_config(args)

    if not config_path.exists():
        print(f"Config not found: {config_path}")
        print("Run 'qanot init' first, or set QANOT_CONFIG env var.")
        sys.exit(1)

    config_path = config_path.resolve()

    if foreground:
        # Run in foreground (for Docker, systemd, debugging)
        os.environ["QANOT_CONFIG"] = str(config_path)
        print(LOGO)
        print(f"Config: {config_path}")
        print()
        from qanot.main import main as run_main
        asyncio.run(run_main())
        return

    # Check if already running via daemon
    is_running, status_msg = daemon_status(config_path)
    if is_running:
        print(f"Bot is already running")
        print(f"  {_dim(status_msg)}")
        return

    # Auto-install service if not installed yet
    daemon_install(config_path)

    # Start via OS service manager
    print(LOGO)
    ok, msg = daemon_start(config_path)
    if ok:
        print(f"  {_green('\u2713')} {msg}")
        print(f"  Logs:   {_cyan('qanot logs')}")
        print(f"  Status: {_cyan('qanot status')}")
        print(f"  Stop:   {_cyan('qanot stop')}")
    else:
        print(f"  {_red('\u2717')} {msg}")
        print(f"  {_dim('Falling back to subprocess mode...')}")
        _start_subprocess(config_path)
    print()


def cmd_stop(args: list[str]) -> None:
    """Stop the bot via OS service manager."""
    from qanot.daemon import daemon_stop, daemon_status

    config_path = _resolve_config(args).resolve()

    # Try daemon stop first
    is_running, _ = daemon_status(config_path)
    if is_running:
        ok, msg = daemon_stop(config_path)
        if ok:
            print(f"  {_green('\u2713')} {msg}")
        else:
            print(f"  {_red('\u2717')} {msg}")
        return

    # Fallback: check PID file (subprocess mode)
    pid_path = _pid_file(config_path)
    running, pid = _is_running(pid_path)
    if running:
        os.kill(pid, signal.SIGTERM)
        pid_path.unlink(missing_ok=True)
        print(f"  {_green('\u2713')} Bot stopped (PID {pid})")
    else:
        print("Bot is not running.")


def cmd_restart(args: list[str]) -> None:
    """Restart the bot."""
    cmd_stop(args)
    cmd_start(args)


def cmd_logs(args: list[str]) -> None:
    """Show bot logs (tail -f)."""
    import subprocess

    config_path = _resolve_config(args).resolve()
    log_path = _log_file(config_path)

    if not log_path.exists():
        print("No log file found. Is the bot running?")
        return

    lines = "50"
    for a in args:
        if a.startswith("-n"):
            lines = a[2:] or "50"

    try:
        subprocess.run(["tail", "-f", "-n", lines, str(log_path)])
    except KeyboardInterrupt:
        pass


def cmd_status(args: list[str]) -> None:
    """Check bot status via OS service manager."""
    from qanot.daemon import daemon_status

    config_path = _resolve_config(args).resolve()

    is_running, msg = daemon_status(config_path)
    if is_running:
        print(f"  {_green('\u25cf')} {msg}")
    else:
        # Fallback: check PID file
        pid_path = _pid_file(config_path)
        running, pid = _is_running(pid_path)
        if running:
            print(f"  {_green('\u25cf')} Bot is running via subprocess (PID {pid})")
        else:
            print(f"  {_red('\u25cf')} Bot is not running")
            print(f"  {_dim(msg)}")
