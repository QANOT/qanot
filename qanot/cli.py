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

# ANSI color helpers (no external deps)
def _green(text: str) -> str: return f"\033[92m{text}\033[0m"
def _red(text: str) -> str: return f"\033[91m{text}\033[0m"
def _yellow(text: str) -> str: return f"\033[93m{text}\033[0m"
def _cyan(text: str) -> str: return f"\033[96m{text}\033[0m"
def _bold(text: str) -> str: return f"\033[1m{text}\033[0m"
def _dim(text: str) -> str: return f"\033[2m{text}\033[0m"


# ── Provider/model definitions ──────────────────────────────

AI_PROVIDERS = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "models": [
            ("claude-sonnet-4-6", "Claude Sonnet 4.6 — fast, recommended"),
            ("claude-opus-4-6", "Claude Opus 4.6 — most capable"),
            ("claude-haiku-4-5-20251001", "Claude Haiku 4.5 — cheapest"),
        ],
        "default_model": "claude-sonnet-4-6",
        "key_hint": "sk-ant-... or sk-ant-oat...",
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "models": [
            ("gpt-4.1", "GPT-4.1 — latest, recommended"),
            ("gpt-4.1-mini", "GPT-4.1 Mini — fast & cheap"),
            ("gpt-4o", "GPT-4o — multimodal"),
            ("gpt-4o-mini", "GPT-4o Mini — cheapest"),
        ],
        "default_model": "gpt-4.1",
        "key_hint": "sk-...",
    },
    "gemini": {
        "label": "Google Gemini",
        "models": [
            ("gemini-2.5-flash", "Gemini 2.5 Flash — fast, recommended"),
            ("gemini-2.5-pro", "Gemini 2.5 Pro — most capable"),
            ("gemini-2.0-flash", "Gemini 2.0 Flash — cheapest"),
        ],
        "default_model": "gemini-2.5-flash",
        "key_hint": "AIza...",
    },
    "groq": {
        "label": "Groq (Llama/Qwen)",
        "models": [
            ("llama-3.3-70b-versatile", "Llama 3.3 70B — recommended"),
            ("llama-3.1-8b-instant", "Llama 3.1 8B — fastest"),
            ("qwen/qwen3-32b", "Qwen 3 32B"),
        ],
        "default_model": "llama-3.3-70b-versatile",
        "key_hint": "gsk_...",
    },
}

VOICE_PROVIDERS = {
    "muxlisa": {
        "label": "Muxlisa.uz (Uzbek native, OGG support)",
        "key_hint": "API key from muxlisa.uz",
    },
    "kotib": {
        "label": "KotibAI (6 voices, multi-language)",
        "key_hint": "JWT token from developer.kotib.ai",
    },
}


# ── Input helpers ────────────────────────────────────────────

def _prompt(text: str, default: str = "") -> str:
    """Prompt with optional default value."""
    if default:
        raw = input(f"  {text} [{_dim(default)}]: ").strip()
        return raw if raw else default
    return input(f"  {text}: ").strip()


def _prompt_secret(text: str, hint: str = "") -> str:
    """Prompt for a secret value (API key, token)."""
    display = f"  {text}"
    if hint:
        display += f" {_dim(f'({hint})')}"
    display += ": "
    try:
        import getpass
        return getpass.getpass(display).strip()
    except Exception:
        return input(display).strip()


def _prompt_select(text: str, options: list[tuple[str, str]], multi: bool = False) -> list[str]:
    """Show numbered menu, return selected keys."""
    print(f"\n  {text}")
    for i, (key, label) in enumerate(options, 1):
        print(f"    {_cyan(str(i))}. {label}")

    if multi:
        raw = input(f"  Select {_dim('(comma-separated, e.g. 1,3)')}: ").strip()
        indices = []
        for part in raw.replace(" ", "").split(","):
            try:
                idx = int(part) - 1
                if 0 <= idx < len(options):
                    indices.append(idx)
            except ValueError:
                pass
        if not indices:
            indices = [0]  # Default to first
        return [options[i][0] for i in indices]
    else:
        raw = input(f"  Select {_dim(f'(1-{len(options)}, default: 1)')}: ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return [options[idx][0]]
        except ValueError:
            pass
        return [options[0][0]]


def _prompt_yn(text: str, default: bool = False) -> bool:
    """Yes/no prompt."""
    hint = "Y/n" if default else "y/N"
    raw = input(f"  {text} ({hint}): ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


# ── Validation helpers ───────────────────────────────────────

def _validate_bot_token(token: str) -> tuple[bool, str, str]:
    """Validate Telegram bot token via getMe. Returns (ok, bot_name, username)."""
    import urllib.request
    import urllib.error
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                bot = data["result"]
                return True, bot.get("first_name", ""), bot.get("username", "")
    except Exception:
        pass
    return False, "", ""


def _validate_api_key(provider: str, api_key: str) -> bool:
    """Quick validation of API key by making a minimal request."""
    import urllib.request
    import urllib.error

    try:
        if provider == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            # OAuth tokens use different auth
            if "sk-ant-oat" in api_key:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "oauth-2025-04-20",
                    "content-type": "application/json",
                }
            body = json.dumps({
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200

        elif provider == "openai":
            url = "https://api.openai.com/v1/models"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200

        elif provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200

        elif provider == "groq":
            url = "https://api.groq.com/openai/v1/models"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200

    except urllib.error.HTTPError as e:
        # 401/403 = bad key, but 400/429 = key works (just bad request or rate limit)
        if e.code in (400, 429):
            return True
    except Exception:
        pass
    return False


# ── Init wizard ──────────────────────────────────────────────

def cmd_init(args: list[str]) -> None:
    """Interactive setup wizard for new Qanot project."""
    target = Path(args[0]) if args else Path.cwd()
    target.mkdir(parents=True, exist_ok=True)

    config_path = target / "config.json"
    if config_path.exists():
        if not _prompt_yn(f"config.json already exists in {target}. Overwrite?"):
            return

    print(LOGO)
    print(_bold("  Welcome to Qanot AI setup!\n"))

    # ── Step 1: Telegram bot token ──
    print(_bold("  Step 1: Telegram Bot"))
    bot_token = ""
    bot_name = ""
    while True:
        bot_token = _prompt_secret("Bot token (from @BotFather)")
        if not bot_token:
            print(_red("    Bot token is required."))
            continue

        print("    Validating...", end=" ", flush=True)
        ok, name, username = _validate_bot_token(bot_token)
        if ok:
            bot_name = name
            print(_green(f"✓ @{username} ({name})"))
            break
        else:
            print(_red("✗ Invalid token. Check with @BotFather."))

    # ── Step 2: AI providers ──
    print(f"\n{_bold('  Step 2: AI Providers')}")
    provider_options = [(k, v["label"]) for k, v in AI_PROVIDERS.items()]
    selected_providers = _prompt_select(
        "Which AI providers do you want to use?",
        provider_options,
        multi=True,
    )

    providers_config: list[dict] = []
    primary_provider = selected_providers[0]
    primary_model = ""

    for prov in selected_providers:
        info = AI_PROVIDERS[prov]
        print(f"\n  {_bold(info['label'])}")

        # Ask for API key
        api_key = ""
        while True:
            api_key = _prompt_secret("API key", info["key_hint"])
            if not api_key:
                print(_red("    API key is required."))
                continue

            print("    Validating...", end=" ", flush=True)
            if _validate_api_key(prov, api_key):
                print(_green("✓ Valid"))
                break
            else:
                print(_red("✗ Invalid key."))
                if _prompt_yn("Try again?", default=True):
                    continue
                else:
                    print(_yellow("    Skipping validation, saving as-is."))
                    break

        # Ask for model
        model_options = [(m, desc) for m, desc in info["models"]]
        selected_model = _prompt_select(
            f"Default model for {info['label']}:",
            model_options,
        )[0]

        if prov == primary_provider:
            primary_model = selected_model

        providers_config.append({
            "name": f"{prov}-main",
            "provider": prov,
            "model": selected_model,
            "api_key": api_key,
        })

    # If multiple providers, confirm primary
    if len(selected_providers) > 1:
        primary_options = [(p, AI_PROVIDERS[p]["label"]) for p in selected_providers]
        print()
        primary_provider = _prompt_select(
            "Which provider should be the primary (default)?",
            primary_options,
        )[0]
        # Update primary_model
        for pc in providers_config:
            if pc["provider"] == primary_provider:
                primary_model = pc["model"]
                break

    # Get primary API key
    primary_api_key = ""
    for pc in providers_config:
        if pc["provider"] == primary_provider:
            primary_api_key = pc["api_key"]
            break

    # ── Step 3: Voice (optional) ──
    print(f"\n{_bold('  Step 3: Voice Messages (optional)')}")
    voice_enabled = _prompt_yn("Enable voice message support?")

    voice_provider = "muxlisa"
    voice_api_keys: dict[str, str] = {}
    voice_mode = "off"

    if voice_enabled:
        voice_options = [(k, v["label"]) for k, v in VOICE_PROVIDERS.items()]
        selected_voice = _prompt_select(
            "Which voice providers?",
            voice_options,
            multi=True,
        )
        voice_provider = selected_voice[0]

        for vp in selected_voice:
            vinfo = VOICE_PROVIDERS[vp]
            vkey = _prompt_secret(f"{vinfo['label']} API key", vinfo["key_hint"])
            if vkey:
                voice_api_keys[vp] = vkey

        voice_mode = "inbound"
        print(_green("  ✓ Voice enabled (inbound mode — replies to voice with voice)"))

    # ── Step 4: Web Search (optional) ──
    print(f"\n{_bold('  Step 4: Web Search (optional)')}")
    brave_api_key = ""
    web_search_enabled = _prompt_yn("Enable web search? (free Brave Search API)")
    if web_search_enabled:
        brave_api_key = _prompt_secret("Brave Search API key", "Get free at brave.com/search/api")
        if brave_api_key:
            print(_green("  ✓ Web search enabled"))
        else:
            print(_yellow("  ! Skipped — you can add brave_api_key to config.json later"))

    # ── Step 5: Build config ──
    print(f"\n{_bold('  Step 5: Saving configuration')}")

    config = {
        "bot_token": bot_token,
        "provider": primary_provider,
        "model": primary_model,
        "api_key": primary_api_key,
        "providers": providers_config,
        "owner_name": "",
        "bot_name": bot_name,
        "timezone": "Asia/Tashkent",
        "max_concurrent": 4,
        "compaction_mode": "safeguard",
        "max_context_tokens": 200000,
        "allowed_users": [],
        "response_mode": "stream",
        "stream_flush_interval": 0.8,
        "telegram_mode": "polling",
        "webhook_url": "",
        "webhook_port": 8443,
        "rag_enabled": True,
        "voice_provider": voice_provider,
        "voice_api_key": "",
        "voice_api_keys": voice_api_keys,
        "voice_mode": voice_mode,
        "voice_name": "",
        "voice_language": "",
        "workspace_dir": str(target / "workspace"),
        "sessions_dir": str(target / "sessions"),
        "cron_dir": str(target / "cron"),
        "brave_api_key": brave_api_key,
        "plugins_dir": str(target / "plugins"),
        "plugins": [],
    }

    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False))

    # Create workspace directory with default SOUL.md
    workspace = target / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    soul_path = workspace / "SOUL.md"
    if not soul_path.exists():
        soul_path.write_text(
            f"# {bot_name}\n\n"
            "You are a helpful AI assistant on Telegram.\n"
            "Respond concisely and helpfully.\n"
        )

    # Create other directories
    for d in ("sessions", "cron", "plugins"):
        (target / d).mkdir(parents=True, exist_ok=True)

    print(_green(f"  ✓ Config saved to {config_path}"))
    print(_green(f"  ✓ Workspace created at {workspace}"))

    # Summary
    print(f"\n{_bold('  Setup complete!')}")
    print(f"  Bot: @{bot_name}")
    print(f"  Provider: {AI_PROVIDERS[primary_provider]['label']} ({primary_model})")
    if len(providers_config) > 1:
        others = [pc["provider"] for pc in providers_config if pc["provider"] != primary_provider]
        print(f"  Backup: {', '.join(AI_PROVIDERS[o]['label'] for o in others)}")
    if voice_enabled:
        print(f"  Voice: {VOICE_PROVIDERS[voice_provider]['label']}")
    if brave_api_key:
        print(f"  Web Search: Brave API")
    print()
    print(f"  Run: {_cyan(f'qanot start {target}')}")
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


def cmd_doctor(args: list[str]) -> None:
    """Run health checks on a Qanot installation."""
    fix_mode = "--fix" in args or "--repair" in args
    remaining = [a for a in args if a not in ("--fix", "--repair")]

    # Find config
    config_path = _find_config(remaining)
    if not config_path:
        print(_red("✗ No config.json found."))
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
        print(f"  {_green('✓')} {msg}")

    def _warn(msg: str, hint: str = "") -> None:
        nonlocal warned
        warned += 1
        print(f"  {_yellow('!')} {msg}")
        if hint:
            print(f"    {_dim(hint)}")

    def _fail(msg: str, hint: str = "") -> None:
        nonlocal failed
        failed += 1
        print(f"  {_red('✗')} {msg}")
        if hint:
            print(f"    {_dim(hint)}")

    def _fix(msg: str) -> None:
        print(f"  {_cyan('⚡')} {msg}")

    # ── 1. Config validation ──────────────────────────────
    print(_bold("Config"))
    try:
        raw_text = config_path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
        _ok(f"Valid JSON: {config_path}")
    except json.JSONDecodeError as e:
        _fail(f"Invalid JSON: {e}")
        print()
        print(_red(f"Cannot continue — fix {config_path} first."))
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
        _warn("allowed_users is empty — bot is PUBLIC", "Add your Telegram user ID for security")

    # Validate provider
    provider = raw.get("provider", "anthropic")
    valid_providers = {"anthropic", "openai", "gemini", "groq"}
    if provider in valid_providers:
        _ok(f"Provider: {provider}")
    else:
        _fail(f"Unknown provider: {provider}", f"Valid: {', '.join(valid_providers)}")

    print()

    # ── 2. Bot token health ───────────────────────────────
    print(_bold("Telegram"))
    bot_token = raw.get("bot_token", "")
    if bot_token and bot_token != "YOUR_TELEGRAM_BOT_TOKEN":
        try:
            import urllib.request
            url = f"https://api.telegram.org/bot{bot_token}/getMe"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if data.get("ok"):
                    bot_info = data["result"]
                    _ok(f"Bot connected: @{bot_info.get('username', '?')} ({bot_info.get('first_name', '?')})")
                else:
                    _fail("Bot token invalid — getMe returned not ok")
        except Exception as e:
            _fail(f"Bot token check failed: {e}")
    else:
        _fail("Bot token not configured")

    print()

    # ── 3. Workspace integrity ────────────────────────────
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

    print()

    # ── 4. Plugin health ──────────────────────────────────
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
                print(f"  {_dim('–')} {pname} {_dim('(disabled)')}")
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

    print()

    # ── 5. Cron / heartbeat ───────────────────────────────
    print(_bold("Cron & Heartbeat"))
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
                _warn("No heartbeat job — self-healing disabled", "Will be auto-created on start")
        except json.JSONDecodeError:
            _fail("jobs.json is invalid JSON")
            if fix_mode:
                jobs_file.write_text("[]", encoding="utf-8")
                _fix("Reset jobs.json to empty array")
    else:
        _ok("No cron jobs yet (heartbeat will auto-create on start)")

    heartbeat_md = ws_dir / "HEARTBEAT.md"
    if heartbeat_md.exists():
        lines = [l for l in heartbeat_md.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        if lines:
            _ok(f"HEARTBEAT.md has {len(lines)} check items")
        else:
            _warn("HEARTBEAT.md is empty — heartbeat will skip API calls")
    else:
        _warn("HEARTBEAT.md not found", "Will be created on first start")

    print()

    # ── 6. Session cleanup ────────────────────────────────
    print(_bold("Sessions"))
    sessions_dir = Path(raw.get("sessions_dir", "/data/sessions"))
    if sessions_dir.exists():
        session_files = list(sessions_dir.glob("*.jsonl"))
        total_size = sum(f.stat().st_size for f in session_files)
        _ok(f"{len(session_files)} session file(s), {total_size / 1024 / 1024:.1f} MB total")

        if total_size > 100 * 1024 * 1024:  # > 100MB
            _warn(f"Sessions using {total_size / 1024 / 1024:.0f} MB", "Consider 'qanot backup' and cleanup")

        # Check for stale sessions (> 30 days old)
        import time
        now = time.time()
        stale = [f for f in session_files if now - f.stat().st_mtime > 30 * 86400]
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

    print()

    # ── 7. Voice config ───────────────────────────────────
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
            _fail(f"{voice_provider} API key missing — voice won't work")

    print()

    # ── 8. Dependencies ───────────────────────────────────
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
            _ok(f"{module} — {label}")
        except ImportError:
            _fail(f"{module} missing — {label}", f"pip install {module}")

    for module, (pkg, label) in optional_deps.items():
        try:
            __import__(module)
            _ok(f"{pkg} — {label}")
        except ImportError:
            _warn(f"{pkg} not installed — {label}", f"pip install {pkg}")

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


def _find_config(args: list[str]) -> Path | None:
    """Find config.json from args or defaults."""
    if args:
        path = Path(args[0])
        if path.is_dir():
            path = path / "config.json"
        return path if path.exists() else None

    env_path = os.environ.get("QANOT_CONFIG")
    if env_path and Path(env_path).exists():
        return Path(env_path)
    if Path("config.json").exists():
        return Path("config.json")
    if Path("/data/config.json").exists():
        return Path("/data/config.json")
    return None


def cmd_backup(args: list[str]) -> None:
    """Export workspace + sessions + cron to a timestamped .tar.gz archive."""
    import tarfile
    from datetime import datetime

    # Find config
    remaining = [a for a in args if not a.startswith("--")]
    config_path = _find_config(remaining)
    if not config_path:
        print(_red("✗ No config.json found."))
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
        (Path(raw.get(config_key, project_dir / name)), name)
        for config_key, name in backup_dirs_spec
        if Path(raw.get(config_key, project_dir / name)).exists()
    ]

    # Always include config.json
    if not dirs_to_backup and not config_path.exists():
        print(_red("✗ Nothing to back up."))
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
    print(f"  {_green('✓')} Backup saved: {output_path}")
    print(f"  {_dim(f'{file_count} files, {size_mb:.1f} MB')}")
    print()


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
            print(f"  {_red(pname)} [{status}] — NOT FOUND")

    # Show discovered but unconfigured plugins
    if plugins_dir.exists():
        discovered = set()
        for d in plugins_dir.iterdir():
            if d.is_dir() and (d / "plugin.py").exists():
                discovered.add(d.name)

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


def cmd_version() -> None:
    from qanot import __version__
    print(f"qanot {__version__}")


def cmd_help() -> None:
    print(LOGO)
    print("Usage: qanot <command> [args]")
    print()
    print("Commands:")
    print("  init [dir]         Interactive setup wizard (creates config.json)")
    print("  start [path]       Start agent (path to config.json or directory)")
    print("  doctor [path]      Health checks (--fix to auto-repair)")
    print("  backup [path]      Export workspace + sessions to .tar.gz")
    print("  plugin new <name>  Scaffold a new plugin")
    print("  plugin list        List installed plugins")
    print("  version            Show version")
    print("  help               Show this help")
    print()
    print("Examples:")
    print("  qanot init mybot")
    print("  qanot start mybot")
    print("  qanot doctor mybot")
    print("  qanot doctor --fix")
    print("  qanot backup mybot")
    print("  qanot plugin new weather")
    print()


def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] == "help" or args[0] == "--help":
        cmd_help()
    elif args[0] == "init":
        cmd_init(args[1:])
    elif args[0] == "start":
        cmd_start(args[1:])
    elif args[0] == "doctor":
        cmd_doctor(args[1:])
    elif args[0] == "backup":
        cmd_backup(args[1:])
    elif args[0] == "plugin":
        cmd_plugin(args[1:])
    elif args[0] == "version" or args[0] == "--version":
        cmd_version()
    else:
        # Default: treat as start
        cmd_start(args)


if __name__ == "__main__":
    main()
