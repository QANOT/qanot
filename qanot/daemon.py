"""Cross-platform daemon management for Qanot AI.

Generates and manages OS-native service files:
- Linux: systemd user service (~/.config/systemd/user/qanot.service)
- macOS: launchd plist (~/Library/LaunchAgents/com.qanot.plist)
- Windows: schtasks (Scheduled Task at login)
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def _detect_platform() -> str:
    """Detect the current platform."""
    system = platform.system().lower()
    if system == "linux":
        return "linux"
    elif system == "darwin":
        return "macos"
    elif system == "windows":
        return "windows"
    return "unknown"


def _qanot_bin() -> str:
    """Find the qanot executable path."""
    qanot = shutil.which("qanot")
    if qanot:
        return qanot
    return f"{sys.executable} -m qanot"


# ── Service file paths ──────────────────────────────────────

def _systemd_service_path(name: str = "qanot") -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"


def _launchd_plist_path(name: str = "qanot") -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"com.{name}.plist"


def _service_name(config_path: Path) -> str:
    """Derive a service name from the config path."""
    parent = config_path.parent.name
    if parent and parent not in (".", "/", "data"):
        return f"qanot-{parent}"
    return "qanot"


def _task_name(service_name: str) -> str:
    """Derive a Windows Scheduled Task name from the service name."""
    return f"Qanot_{service_name}"


# ── Generators ───────────────────────────────────────────────

def _generate_systemd(config_path: Path) -> str:
    """Generate a systemd user service file."""
    qanot = _qanot_bin()
    config = str(config_path.resolve())
    working_dir = str(config_path.resolve().parent)

    return f"""[Unit]
Description=Qanot AI Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={qanot} start --foreground {config}
WorkingDirectory={working_dir}
Environment=QANOT_CONFIG={config}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=300

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={working_dir}
PrivateTmp=yes

# Resource limits
MemoryMax=2G
CPUQuota=200%

[Install]
WantedBy=default.target
"""


def _generate_launchd(config_path: Path) -> str:
    """Generate a macOS LaunchAgent plist."""
    qanot = _qanot_bin()
    config = str(config_path.resolve())
    working_dir = str(config_path.resolve().parent)
    log_path = str(config_path.resolve().parent / "qanot.log")
    name = _service_name(config_path)

    # Split command for ProgramArguments
    parts = qanot.split()
    args_xml = "\n".join(f"    <string>{p}</string>" for p in parts)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.{name}</string>

    <key>ProgramArguments</key>
    <array>
{args_xml}
    <string>start</string>
    <string>--foreground</string>
    <string>{config}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{working_dir}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>QANOT_CONFIG</key>
        <string>{config}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{log_path}</string>

    <key>StandardErrorPath</key>
    <string>{log_path}</string>

    <key>ThrottleInterval</key>
    <integer>5</integer>
</dict>
</plist>
"""


# ── Install ──────────────────────────────────────────────────

def daemon_install(config_path: Path) -> tuple[bool, str]:
    """Install OS-native service for Qanot AI.

    Returns (success, message).
    """
    plat = _detect_platform()
    config_path = config_path.resolve()
    name = _service_name(config_path)

    if plat == "linux":
        service_path = _systemd_service_path(name)
        service_path.parent.mkdir(parents=True, exist_ok=True)
        service_path.write_text(_generate_systemd(config_path), encoding="utf-8")

        # Reload systemd
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)

        # Enable linger for headless servers
        user = os.environ.get("USER", "")
        if user:
            subprocess.run(["loginctl", "enable-linger", user], check=False)

        return True, (
            f"Installed: {service_path}\n"
            f"  Start:   qanot daemon start\n"
            f"  Enable:  systemctl --user enable {name}"
        )

    elif plat == "macos":
        plist_path = _launchd_plist_path(name)
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(_generate_launchd(config_path), encoding="utf-8")

        return True, (
            f"Installed: {plist_path}\n"
            f"  Start:   qanot daemon start\n"
            f"  Auto-starts on login (RunAtLoad=true)"
        )

    elif plat == "windows":
        qanot = _qanot_bin()
        config = str(config_path)
        task_name = f"Qanot_{name}"

        result = subprocess.run(
            [
                "schtasks", "/create",
                "/tn", task_name,
                "/tr", f'"{qanot}" start --foreground "{config}"',
                "/sc", "onlogon",
                "/rl", "limited",
                "/f",
            ],
            capture_output=True, text=True,
        )

        if result.returncode == 0:
            return True, (
                f"Installed: Windows Scheduled Task '{task_name}'\n"
                f"  Runs on login automatically\n"
                f"  Start:   qanot daemon start"
            )
        else:
            return False, f"Failed to create scheduled task: {result.stderr}"

    return False, f"Unsupported platform: {plat}"


# ── Uninstall ────────────────────────────────────────────────

def daemon_uninstall(config_path: Path) -> tuple[bool, str]:
    """Remove OS-native service."""
    plat = _detect_platform()
    config_path = config_path.resolve()
    name = _service_name(config_path)

    if plat == "linux":
        service_path = _systemd_service_path(name)
        subprocess.run(["systemctl", "--user", "stop", name], check=False)
        subprocess.run(["systemctl", "--user", "disable", name], check=False)
        if service_path.exists():
            service_path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        return True, f"Removed: {service_path}"

    elif plat == "macos":
        plist_path = _launchd_plist_path(name)
        label = f"com.{name}"
        subprocess.run(["launchctl", "unload", str(plist_path)], check=False)
        if plist_path.exists():
            plist_path.unlink()
        return True, f"Removed: {plist_path}"

    elif plat == "windows":
        task_name = f"Qanot_{name}"
        subprocess.run(
            ["schtasks", "/delete", "/tn", task_name, "/f"],
            check=False,
        )
        return True, f"Removed: Windows Scheduled Task '{task_name}'"

    return False, f"Unsupported platform: {plat}"


# ── Start/Stop/Restart/Status ────────────────────────────────

def daemon_start(config_path: Path) -> tuple[bool, str]:
    """Start the daemon via OS service manager."""
    plat = _detect_platform()
    name = _service_name(config_path.resolve())

    if plat == "linux":
        result = subprocess.run(
            ["systemctl", "--user", "start", name],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, f"Started {name}"
        return False, f"Failed: {result.stderr.strip()}"

    elif plat == "macos":
        plist_path = _launchd_plist_path(name)
        if not plist_path.exists():
            return False, f"Service not installed. Run 'qanot daemon install' first."
        result = subprocess.run(
            ["launchctl", "load", str(plist_path)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, f"Started {name}"
        return False, f"Failed: {result.stderr.strip()}"

    elif plat == "windows":
        task_name = f"Qanot_{name}"
        result = subprocess.run(
            ["schtasks", "/run", "/tn", task_name],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, f"Started {task_name}"
        return False, f"Failed: {result.stderr.strip()}"

    return False, f"Unsupported platform: {plat}"


def daemon_stop(config_path: Path) -> tuple[bool, str]:
    """Stop the daemon via OS service manager."""
    plat = _detect_platform()
    name = _service_name(config_path.resolve())

    if plat == "linux":
        result = subprocess.run(
            ["systemctl", "--user", "stop", name],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, f"Stopped {name}"
        return False, f"Failed: {result.stderr.strip()}"

    elif plat == "macos":
        plist_path = _launchd_plist_path(name)
        result = subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, f"Stopped {name}"
        return False, f"Failed: {result.stderr.strip()}"

    elif plat == "windows":
        task_name = f"Qanot_{name}"
        result = subprocess.run(
            ["schtasks", "/end", "/tn", task_name],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, f"Stopped {task_name}"
        return False, f"Failed: {result.stderr.strip()}"

    return False, f"Unsupported platform: {plat}"


def daemon_restart(config_path: Path) -> tuple[bool, str]:
    """Restart the daemon."""
    plat = _detect_platform()
    name = _service_name(config_path.resolve())

    if plat == "linux":
        result = subprocess.run(
            ["systemctl", "--user", "restart", name],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, f"Restarted {name}"
        return False, f"Failed: {result.stderr.strip()}"

    elif plat == "macos":
        daemon_stop(config_path)
        return daemon_start(config_path)

    elif plat == "windows":
        daemon_stop(config_path)
        return daemon_start(config_path)

    return False, f"Unsupported platform: {plat}"


def daemon_status(config_path: Path) -> tuple[bool, str]:
    """Check daemon status."""
    plat = _detect_platform()
    name = _service_name(config_path.resolve())

    if plat == "linux":
        result = subprocess.run(
            ["systemctl", "--user", "is-active", name],
            capture_output=True, text=True,
        )
        state = result.stdout.strip()
        is_running = state == "active"

        # Get more details
        detail = subprocess.run(
            ["systemctl", "--user", "status", name, "--no-pager", "-l"],
            capture_output=True, text=True,
        )
        return is_running, detail.stdout or state

    elif plat == "macos":
        label = f"com.{name}"
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True,
        )
        for line in result.stdout.splitlines():
            if label in line:
                parts = line.split()
                pid = parts[0] if parts[0] != "-" else None
                if pid:
                    return True, f"Running (PID {pid})"
                return False, "Installed but not running"
        return False, "Not installed"

    elif plat == "windows":
        task_name = f"Qanot_{name}"
        result = subprocess.run(
            ["schtasks", "/query", "/tn", task_name, "/fo", "list"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            is_running = "Running" in result.stdout
            return is_running, result.stdout.strip()
        return False, "Not installed"

    return False, f"Unsupported platform: {plat}"
