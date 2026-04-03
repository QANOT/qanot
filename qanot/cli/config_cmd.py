"""Configuration management commands: show, set, add-provider, remove-provider."""

from __future__ import annotations

import json
from pathlib import Path

from qanot.cli.utils import (
    AI_PROVIDERS,
    LOGO,
    _bold,
    _cyan,
    _detect_ollama,
    _dim,
    _find_config,
    _green,
    _list_ollama_models,
    _prompt,
    _prompt_secret,
    _prompt_select,
    _red,
    _validate_api_key,
    _yellow,
)


def cmd_config(args: list[str]) -> None:
    """Manage bot configuration after initial setup."""
    if not args:
        _config_help()
        return

    subcmd = args[0]
    if subcmd == "show":
        _config_show(args[1:])
    elif subcmd == "set":
        _config_set(args[1:])
    elif subcmd == "add-provider":
        _config_add_provider(args[1:])
    elif subcmd == "remove-provider":
        _config_remove_provider(args[1:])
    else:
        print(_red(f"Unknown config command: {subcmd}"))
        _config_help()


def _config_help() -> None:
    print(LOGO)
    print("Usage: qanot config <command> [args]")
    print()
    print("Commands:")
    print("  show                  Show current configuration")
    print("  set <key> <value>     Set a config value")
    print("  add-provider          Add a backup AI provider (interactive)")
    print("  remove-provider       Remove an AI provider")
    print()
    print("Examples:")
    print("  qanot config show")
    print("  qanot config set model claude-sonnet-4-6")
    print("  qanot config set response_mode partial")
    print("  qanot config add-provider")
    print()


def _config_show(args: list[str]) -> None:
    """Show current configuration."""
    config_path = _find_config(args)
    if not config_path:
        print(_red("No config.json found."))
        return

    raw = json.loads(config_path.read_text(encoding="utf-8"))

    print(LOGO)
    print(_bold("Current Configuration"))
    print(f"  {_dim(str(config_path))}")
    print()

    # Core
    print(_bold("  Core"))
    print(f"    Provider:  {_cyan(raw.get('provider', '?'))}")
    print(f"    Model:     {_cyan(raw.get('model', '?'))}")
    print(f"    Bot:       {raw.get('bot_name', '?')}")
    print(f"    Response:  {raw.get('response_mode', '?')}")
    print()

    # Providers
    providers = raw.get("providers", [])
    print(_bold(f"  Providers ({len(providers)})"))
    if not providers:
        print(f"    {_dim('None configured')}")
    for p in providers:
        is_primary = p.get("provider") == raw.get("provider")
        tag = _green(" (primary)") if is_primary else ""
        key_preview = p.get("api_key", "")[:15] + "..." if p.get("api_key") else _red("no key")
        print(f"    {_cyan(p.get('name', '?'))}: {p.get('model', '?')} [{key_preview}]{tag}")
    print()

    # Features
    print(_bold("  Features"))
    print(f"    Voice:      {raw.get('voice_mode', 'off')}")
    print(f"    RAG:        {'on' if raw.get('rag_enabled') else 'off'}")
    print(f"    Web Search: {'on' if raw.get('brave_api_key') else 'off'}")
    print(f"    Plugins:    {len(raw.get('plugins', []))}")
    print()


def _config_set(args: list[str]) -> None:
    """Set a config value."""
    if len(args) < 2:
        print(_red("Usage: qanot config set <key> <value>"))
        print()
        print("Common keys:")
        print("  model              AI model name")
        print("  provider           Primary provider (anthropic/openai/gemini/groq)")
        print("  response_mode      stream / partial / blocked")
        print("  api_key            Primary API key")
        print("  max_context_tokens Max context window size")
        print("  brave_api_key      Brave Search API key")
        return

    config_path = _find_config([])
    if not config_path:
        print(_red("No config.json found."))
        return

    key = args[0]
    value = " ".join(args[1:])

    raw = json.loads(config_path.read_text(encoding="utf-8"))

    # Type coercion
    if value.lower() == "true":
        value = True
    elif value.lower() == "false":
        value = False
    else:
        for _converter in (int, float):
            try:
                value = _converter(value)
                break
            except ValueError:
                pass  # Keep as string

    old_value = raw.get(key, _dim("(not set)"))
    raw[key] = value
    from qanot.utils import atomic_write
    atomic_write(config_path, json.dumps(raw, indent=2, ensure_ascii=False))
    print(f"  {_green('\u2713')} {key}: {old_value} \u2192 {_cyan(str(value))}")
    print(f"  {_dim('Restart bot for changes to take effect: qanot restart')}")


def _config_add_provider(args: list[str]) -> None:
    """Interactively add a backup AI provider."""
    config_path = _find_config(args)
    if not config_path:
        print(_red("No config.json found."))
        return

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    existing_providers = {p.get("provider") for p in raw.get("providers", [])}
    existing_names = {p.get("name", "") for p in raw.get("providers", [])}

    # Filter out already configured providers (check both provider type and ollama by name)
    available = {}
    for k, v in AI_PROVIDERS.items():
        if k == "ollama":
            if "ollama-main" not in existing_names:
                available[k] = v
        elif k not in existing_providers:
            available[k] = v
    if not available:
        print(_yellow("All providers already configured."))
        return

    print(LOGO)
    print(_bold("Add Backup Provider"))
    print(f"  {_dim('Failover: if primary fails, bot auto-switches to backup')}")
    print()

    provider_options = [(k, v["label"]) for k, v in available.items()]
    selected = _prompt_select("Which provider to add?", provider_options)[0]
    info = AI_PROVIDERS[selected]

    # Ollama: special handling
    if selected == "ollama":
        base_url = info.get("base_url", "http://localhost:11434/v1")
        ollama_api = base_url.replace("/v1", "")
        print("  Checking Ollama...", end=" ", flush=True)
        if _detect_ollama(ollama_api):
            print(_green("\u2713 Running"))
            models = _list_ollama_models(ollama_api)
            model_options = models if models else [
                ("qwen3.5:35b", "Qwen 3.5 35B \u2014 recommended"),
                ("qwen3.5:9b", "Qwen 3.5 9B \u2014 lighter"),
            ]
        else:
            print(_yellow("! Not running"))
            model_options = [("qwen3.5:35b", "Qwen 3.5 35B"), ("qwen3.5:9b", "Qwen 3.5 9B")]

        custom_url = _prompt("Ollama URL", base_url)
        selected_model = _prompt_select("Which model?", model_options)[0]

        if "providers" not in raw:
            raw["providers"] = []
        raw["providers"].append({
            "name": "ollama-main",
            "provider": "openai",
            "model": selected_model,
            "api_key": "ollama",
            "base_url": custom_url,
        })
        raw.setdefault("provider", "openai")
        raw.setdefault("model", selected_model)
        from qanot.utils import atomic_write
    atomic_write(config_path, json.dumps(raw, indent=2, ensure_ascii=False))
        print(f"\n  {_green('\u2713')} Added Ollama ({selected_model})")
        print(f"  {_dim('Restart bot: qanot restart')}")
        print()
        return

    # Cloud providers: API key
    api_key = _prompt_secret(f"{info['label']} API key", info["key_hint"])
    if not api_key:
        print(_red("API key is required."))
        return

    # Validate
    print("  Validating...", end=" ", flush=True)
    if _validate_api_key(selected, api_key):
        print(_green("\u2713 Valid"))
    else:
        print(_yellow("? Could not validate (saving anyway)"))

    # Model
    model_options = info["models"]
    selected_model = _prompt_select(
        f"Model for {info['label']}:",
        model_options,
    )[0]

    # Save
    if "providers" not in raw:
        raw["providers"] = []
    raw["providers"].append({
        "name": f"{selected}-main",
        "provider": selected,
        "model": selected_model,
        "api_key": api_key,
    })
    from qanot.utils import atomic_write
    atomic_write(config_path, json.dumps(raw, indent=2, ensure_ascii=False))

    print()
    print(f"  {_green('\u2713')} Added {info['label']} ({selected_model})")
    print(f"  {_dim('Restart bot for changes to take effect: qanot restart')}")
    print()


def _config_remove_provider(args: list[str]) -> None:
    """Remove a configured provider."""
    config_path = _find_config(args)
    if not config_path:
        print(_red("No config.json found."))
        return

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    providers = raw.get("providers", [])

    if len(providers) <= 1:
        print(_red("Cannot remove the only provider."))
        return

    print(LOGO)
    print(_bold("Remove Provider"))
    print()

    options = [(p["name"], f"{p['provider']} / {p.get('model', '?')}") for p in providers]
    selected = _prompt_select("Which provider to remove?", options)[0]

    # Don't allow removing primary
    removed = None
    for p in providers:
        if p["name"] == selected:
            if p["provider"] == raw.get("provider"):
                print(_red("Cannot remove the primary provider. Change primary first:"))
                print(f"  {_dim('qanot config set provider <other_provider>')}")
                return
            removed = p
            break

    if removed:
        raw["providers"] = [p for p in providers if p["name"] != selected]
        from qanot.utils import atomic_write
    atomic_write(config_path, json.dumps(raw, indent=2, ensure_ascii=False))
        print(f"  {_green('\u2713')} Removed {removed['provider']} ({removed.get('model', '?')})")
        print(f"  {_dim('Restart bot for changes to take effect: qanot restart')}")
