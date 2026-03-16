"""Shared CLI helpers: colors, prompts, validation, config resolution."""

from __future__ import annotations

import json
import os
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
    "ollama": {
        "label": "Ollama (Local — free, private)",
        "models": [],  # Populated dynamically from ollama list
        "default_model": "",
        "key_hint": "No API key needed",
        "base_url": "http://localhost:11434/v1",
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
    "aisha": {
        "label": "Aisha AI (STT+TTS — Gulnoza/Jaxongir, mood, uz/en/ru)",
        "key_hint": "API key from aisha.group",
    },
    "whisper": {
        "label": "OpenAI Whisper (STT only — high accuracy, 50+ languages)",
        "key_hint": "sk-proj-... from platform.openai.com",
    },
}


# ── Ollama helpers ───────────────────────────────────────────

def _detect_ollama(base_url: str = "http://localhost:11434") -> bool:
    """Check if Ollama is running locally."""
    import urllib.request
    try:
        req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _list_ollama_models(base_url: str = "http://localhost:11434") -> list[tuple[str, str]]:
    """List available Ollama models. Returns [(model_name, description), ...]."""
    import urllib.request
    try:
        req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            models = []
            for m in data.get("models", []):
                name = m.get("name", "")
                size_gb = m.get("size", 0) / (1024**3)
                desc = f"{name} ({size_gb:.1f} GB)"
                models.append((name, desc))
            return models
    except Exception:
        return []


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
    import re
    import urllib.request
    import urllib.error
    # Bot tokens must match digits:alphanumeric pattern; reject anything else
    # to prevent URL injection / header injection via crafted tokens
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        return False, "", ""
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
    except urllib.error.URLError as e:
        # Network-level errors (DNS, connection refused, timeout)
        # Treat as inconclusive — don't reject a potentially valid key
        # due to network issues
        import socket
        if isinstance(e.reason, socket.timeout):
            return True  # Assume key is valid; network just timed out
        pass
    except Exception:
        pass
    return False


# ── Config resolution helpers ────────────────────────────────

def _resolve_config(args: list[str]) -> Path:
    """Resolve config.json path from args or defaults."""
    # Filter out flags
    positional = [a for a in args if not a.startswith("--")]
    if positional:
        path = Path(positional[0])
        if path.is_dir():
            return path / "config.json"
        return path

    env_path = os.environ.get("QANOT_CONFIG")
    if env_path:
        return Path(env_path)
    if Path("config.json").exists():
        return Path("config.json")
    return Path("/data/config.json")


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
