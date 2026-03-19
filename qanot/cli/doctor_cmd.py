"""Health check diagnostics for Qanot installations."""

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


def cmd_doctor(args: list[str]) -> None:
    """Run health checks on a Qanot installation."""
    fix_mode = "--fix" in args or "--repair" in args
    # Find config
    config_path = _find_config([a for a in args if a not in ("--fix", "--repair")])
    if not config_path:
        print(_red("\u2717 No config.json found."))
        print("  Run 'qanot init' first, or pass the path.")
        sys.exit(1)

    print(LOGO)
    print(_bold("Qanot Doctor"))
    if fix_mode:
        print(_yellow("  Mode: --fix (will auto-repair issues)"))
    print()

    passed = 0
    warned = 0
    failed = 0

    def _ok(msg: str) -> None:
        nonlocal passed
        passed += 1
        print(f"  {_green('\u2713')} {msg}")

    def _warn(msg: str, hint: str = "") -> None:
        nonlocal warned
        warned += 1
        print(f"  {_yellow('!')} {msg}")
        if hint:
            print(f"    {_dim(hint)}")

    def _fail(msg: str, hint: str = "") -> None:
        nonlocal failed
        failed += 1
        print(f"  {_red('\u2717')} {msg}")
        if hint:
            print(f"    {_dim(hint)}")

    def _fix(msg: str) -> None:
        print(f"  {_cyan('\u26a1')} {msg}")

    # ── 1. Config validation ──────────────────────────────
    _check_config(config_path, _ok, _warn, _fail)

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    print()

    # ── 2. Bot token health ───────────────────────────────
    _check_telegram(raw, _ok, _fail)
    print()

    # ── 3. Workspace integrity ────────────────────────────
    _check_workspace(raw, fix_mode, _ok, _warn, _fix)
    print()

    # ── 4. Plugin health ──────────────────────────────────
    _check_plugins(raw, _ok, _warn, _fail)
    print()

    # ── 5. Cron / heartbeat ───────────────────────────────
    _check_cron(raw, fix_mode, _ok, _warn, _fail, _fix)
    print()

    # ── 6. Session cleanup ────────────────────────────────
    _check_sessions(raw, fix_mode, _ok, _warn, _fix)
    print()

    # ── 7. Voice config ───────────────────────────────────
    _check_voice(raw, _ok, _fail)
    print()

    # ── 8. Dependencies ───────────────────────────────────
    _check_dependencies(_ok, _warn, _fail)
    print()

    # ── Summary ───────────────────────────────────────────
    print(_bold("Summary"))
    total = passed + warned + failed
    print(f"  {_green(f'{passed} passed')}, {_yellow(f'{warned} warnings')}, {_red(f'{failed} errors')} ({total} checks)")
    if failed == 0 and warned == 0:
        print(f"\n  {_green('All good! Your bot is healthy.')}")
    elif failed == 0:
        print(f"\n  {_yellow('Bot should work, but review the warnings above.')}")
    else:
        print(f"\n  {_red('Fix the errors above before starting the bot.')}")
        if not fix_mode:
            print(f"  {_dim('Try: qanot doctor --fix')}")
    print()


def _check_config(config_path: Path, _ok, _warn, _fail) -> None:
    """Validate config.json structure and required fields."""
    print(_bold("Config"))
    try:
        raw_text = config_path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
        _ok(f"Valid JSON: {config_path}")
    except json.JSONDecodeError as e:
        _fail(f"Invalid JSON: {e}")
        print()
        print(_red(f"Cannot continue \u2014 fix {config_path} first."))
        sys.exit(1)

    # Required fields
    required_fields = ["bot_token", "api_key"]
    for field in required_fields:
        if raw.get(field) and raw[field] != f"YOUR_{field.upper()}":
            _ok(f"{field} is set")
        else:
            _fail(f"{field} is missing or placeholder", f"Set it in {config_path}")

    # Warn on empty optional fields
    if not raw.get("owner_name"):
        _warn("owner_name is empty", "Bot won't know your name")
    if not raw.get("bot_name"):
        _warn("bot_name is empty", "Bot won't have a persona name")
    if not raw.get("allowed_users"):
        _warn("allowed_users is empty \u2014 bot is PUBLIC", "Add your Telegram user ID for security")

    # Validate provider
    provider = raw.get("provider", "anthropic")
    valid_providers = {"anthropic", "openai", "gemini", "groq"}
    if provider in valid_providers:
        _ok(f"Provider: {provider}")
    else:
        _fail(f"Unknown provider: {provider}", f"Valid: {', '.join(valid_providers)}")


def _check_telegram(raw: dict, _ok, _fail) -> None:
    """Check Telegram bot token validity."""
    print(_bold("Telegram"))
    bot_token = raw.get("bot_token", "")
    if bot_token and bot_token != "YOUR_TELEGRAM_BOT_TOKEN":
        try:
            import urllib.request
            url = f"https://api.telegram.org/bot{bot_token}/getMe"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
                if data.get("ok"):
                    bot_info = data["result"]
                    _ok(f"Bot connected: @{bot_info.get('username', '?')} ({bot_info.get('first_name', '?')})")
                else:
                    _fail("Bot token invalid \u2014 getMe returned not ok")
        except Exception as e:
            _fail(f"Bot token check failed: {e}")
    else:
        _fail("Bot token not configured")


def _check_workspace(raw: dict, fix_mode: bool, _ok, _warn, _fix) -> None:
    """Check workspace directory and critical files."""
    print(_bold("Workspace"))
    ws_dir = Path(raw.get("workspace_dir", "/data/workspace"))
    if ws_dir.exists():
        _ok(f"Workspace exists: {ws_dir}")
    else:
        if fix_mode:
            ws_dir.mkdir(parents=True, exist_ok=True)
            _fix(f"Created workspace: {ws_dir}")
        else:
            _warn(f"Workspace missing: {ws_dir}", "Run 'qanot start' to auto-create, or use --fix")

    # Check critical files
    critical_files = ["SOUL.md", "TOOLS.md", "IDENTITY.md"]
    for fname in critical_files:
        fpath = ws_dir / fname
        if fpath.exists():
            size = fpath.stat().st_size
            if size > 0:
                _ok(f"{fname} ({size:,} bytes)")
            else:
                _warn(f"{fname} is empty")
        else:
            _warn(f"{fname} missing", "Will be created on first start")

    # Check memory dir
    mem_dir = ws_dir / "memory"
    if mem_dir.exists():
        note_count = len(list(mem_dir.glob("*.md")))
        _ok(f"memory/ ({note_count} notes)")
    else:
        if fix_mode:
            mem_dir.mkdir(parents=True, exist_ok=True)
            _fix("Created memory/ directory")
        else:
            _warn("memory/ directory missing")

    # Check directories
    for dir_key in ["sessions_dir", "cron_dir"]:
        dir_path = Path(raw.get(dir_key, ""))
        if dir_path.exists():
            _ok(f"{dir_key}: {dir_path}")
        else:
            if fix_mode:
                dir_path.mkdir(parents=True, exist_ok=True)
                _fix(f"Created {dir_key}: {dir_path}")
            else:
                _warn(f"{dir_key} missing: {dir_path}", "Will be created on first start")


def _check_plugins(raw: dict, _ok, _warn, _fail) -> None:
    """Check plugin health."""
    print(_bold("Plugins"))
    plugins = raw.get("plugins", [])
    if not plugins:
        _ok("No plugins configured")
    else:
        from qanot.plugins.loader import _find_plugin_dir
        plugins_dir = raw.get("plugins_dir", "/data/plugins")
        for pl in plugins:
            pname = pl if isinstance(pl, str) else pl.get("name", "?")
            enabled = pl.get("enabled", True) if isinstance(pl, dict) else True
            if not enabled:
                print(f"  {_dim('\u2013')} {pname} {_dim('(disabled)')}")
                continue

            plugin_dir = _find_plugin_dir(pname, plugins_dir)
            if plugin_dir:
                plugin_py = plugin_dir / "plugin.py"
                manifest = plugin_dir / "plugin.json"
                if plugin_py.exists():
                    if manifest.exists():
                        from qanot.plugins.base import PluginManifest
                        m = PluginManifest.from_file(manifest)
                        _ok(f"{pname} v{m.version}")

                        # Check required config
                        pl_config = pl.get("config", {}) if isinstance(pl, dict) else {}
                        missing = [k for k in m.required_config if not pl_config.get(k)]
                        if missing:
                            _warn(f"  {pname} missing config: {', '.join(missing)}")
                    else:
                        _ok(f"{pname} (no manifest)")
                else:
                    _fail(f"{pname}: plugin.py not found in {plugin_dir}")
            else:
                _fail(f"{pname}: directory not found")


def _check_cron(raw: dict, fix_mode: bool, _ok, _warn, _fail, _fix) -> None:
    """Check cron jobs and heartbeat configuration."""
    print(_bold("Cron & Heartbeat"))
    ws_dir = Path(raw.get("workspace_dir", "/data/workspace"))
    cron_dir = Path(raw.get("cron_dir", "/data/cron"))
    jobs_file = cron_dir / "jobs.json"
    if jobs_file.exists():
        try:
            jobs = json.loads(jobs_file.read_text(encoding="utf-8"))
            _ok(f"{len(jobs)} cron job(s)")
            has_heartbeat = any(j.get("name") == "heartbeat" for j in jobs)
            if has_heartbeat:
                _ok("Heartbeat job configured")
            else:
                _warn("No heartbeat job \u2014 self-healing disabled", "Will be auto-created on start")
        except json.JSONDecodeError:
            _fail("jobs.json is invalid JSON")
            if fix_mode:
                jobs_file.write_text("[]", encoding="utf-8")
                _fix("Reset jobs.json to empty array")
    else:
        _ok("No cron jobs yet (heartbeat will auto-create on start)")

    heartbeat_md = ws_dir / "HEARTBEAT.md"
    if heartbeat_md.exists():
        lines = [s for l in heartbeat_md.read_text(encoding="utf-8").splitlines()
                 if (s := l.strip()) and not s.startswith("#")]
        if lines:
            _ok(f"HEARTBEAT.md has {len(lines)} check items")
        else:
            _warn("HEARTBEAT.md is empty \u2014 heartbeat will skip API calls")
    else:
        _warn("HEARTBEAT.md not found", "Will be created on first start")


def _check_sessions(raw: dict, fix_mode: bool, _ok, _warn, _fix) -> None:
    """Check session files and storage."""
    print(_bold("Sessions"))
    sessions_dir = Path(raw.get("sessions_dir", "/data/sessions"))
    if sessions_dir.exists():
        session_files = list(sessions_dir.glob("*.jsonl"))
        # Cache stat results to avoid double stat() calls
        file_stats = [(f, f.stat()) for f in session_files]
        total_size = sum(st.st_size for _, st in file_stats)
        _ok(f"{len(session_files)} session file(s), {total_size / 1024 / 1024:.1f} MB total")

        if total_size > 100 * 1024 * 1024:  # > 100MB
            _warn(f"Sessions using {total_size / 1024 / 1024:.0f} MB", "Consider 'qanot backup' and cleanup")

        # Check for stale sessions (> 30 days old)
        import time
        now = time.time()
        stale = [f for f, st in file_stats if now - st.st_mtime > 30 * 86400]
        if stale:
            _warn(f"{len(stale)} session(s) older than 30 days")
            if fix_mode:
                archive_dir = sessions_dir / "archive"
                archive_dir.mkdir(exist_ok=True)
                for f in stale:
                    f.rename(archive_dir / f.name)
                _fix(f"Archived {len(stale)} stale sessions to archive/")
    else:
        _ok("No sessions directory yet")


def _check_voice(raw: dict, _ok, _fail) -> None:
    """Check voice configuration."""
    print(_bold("Voice"))
    voice_mode = raw.get("voice_mode", "off")
    voice_provider = raw.get("voice_provider", "muxlisa")
    if voice_mode == "off":
        _ok("Voice is off")
    else:
        _ok(f"Voice mode: {voice_mode}, provider: {voice_provider}")
        # Check API key
        voice_keys = raw.get("voice_api_keys", {})
        key = voice_keys.get(voice_provider, raw.get("voice_api_key", ""))
        if key:
            _ok(f"{voice_provider} API key is set")
        else:
            _fail(f"{voice_provider} API key missing \u2014 voice won't work")


def _check_dependencies(_ok, _warn, _fail) -> None:
    """Check required and optional Python dependencies."""
    print(_bold("Dependencies"))
    deps_check = {
        "aiogram": "Telegram adapter",
        "aiohttp": "HTTP client",
        "apscheduler": "Cron scheduler",
    }
    optional_deps = {
        "PIL": ("Pillow", "Image processing (stickers, photos)"),
        "anthropic": ("anthropic", "Anthropic provider"),
    }
    for module, label in deps_check.items():
        try:
            __import__(module)
            _ok(f"{module} \u2014 {label}")
        except ImportError:
            _fail(f"{module} missing \u2014 {label}", f"pip install {module}")

    for module, (pkg, label) in optional_deps.items():
        try:
            __import__(module)
            _ok(f"{pkg} \u2014 {label}")
        except ImportError:
            _warn(f"{pkg} not installed \u2014 {label}", f"pip install {pkg}")
