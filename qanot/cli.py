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

    # ── Step 4: Build config ──
    print(f"\n{_bold('  Step 4: Saving configuration')}")

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


def cmd_version() -> None:
    from qanot import __version__
    print(f"qanot {__version__}")


def cmd_help() -> None:
    print(LOGO)
    print("Usage: qanot <command> [args]")
    print()
    print("Commands:")
    print("  init [dir]       Interactive setup wizard (creates config.json)")
    print("  start [path]     Start agent (path to config.json or directory)")
    print("  version          Show version")
    print("  help             Show this help")
    print()
    print("Examples:")
    print("  pip install qanot")
    print("  qanot init mybot")
    print("  qanot start mybot")
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
