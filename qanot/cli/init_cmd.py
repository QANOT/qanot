"""Interactive setup wizard for new Qanot project."""

from __future__ import annotations

import json
from pathlib import Path

from qanot.cli.utils import (
    AI_PROVIDERS,
    LOGO,
    VOICE_PROVIDERS,
    _bold,
    _cyan,
    _detect_ollama,
    _dim,
    _green,
    _list_ollama_models,
    _prompt,
    _prompt_secret,
    _prompt_select,
    _prompt_yn,
    _red,
    _validate_api_key,
    _validate_bot_token,
    _yellow,
)


# Plugin config keys — what each plugin needs from the user
_PLUGIN_KEYS: dict[str, list[tuple[str, str, bool]]] = {
    # (key, hint, is_secret)
    "bito": [
        ("api_key", "Bito API kaliti", True),
    ],
    "absmarket": [
        ("api_key", "AbsMarket API kaliti", True),
        ("db_host", "MySQL host (masalan: localhost)", False),
        ("db_user", "MySQL foydalanuvchi", False),
        ("db_password", "MySQL parol", True),
        ("db_name", "MySQL baza nomi", False),
    ],
    "ibox": [
        ("tenant", "ibox tenant (kompaniya ID)", False),
        ("login", "ibox login", False),
        ("password", "ibox parol", True),
    ],
    "moysklad": [
        ("login", "MoySklad login", False),
        ("password", "MoySklad parol", True),
    ],
    "onec": [
        ("base_url", "1C bazaviy URL (masalan: http://server/base)", False),
        ("username", "1C foydalanuvchi", False),
        ("password", "1C parol", True),
    ],
    "amocrm": [
        ("subdomain", "amoCRM subdomain (masalan: mycompany)", False),
        ("client_id", "OAuth client ID", False),
        ("client_secret", "OAuth client secret", True),
        ("access_token", "Access token", True),
        ("refresh_token", "Refresh token", True),
        ("redirect_uri", "Redirect URI", False),
    ],
    "bitrix24": [
        ("domain", "Bitrix24 domain (masalan: myco.bitrix24.uz)", False),
        ("user_id", "Webhook user ID", False),
        ("webhook_code", "Webhook code", True),
    ],
    "eskiz": [
        ("email", "Eskiz email", False),
        ("password", "Eskiz parol", True),
    ],
    "absvision": [
        ("base_url", "AbsVision URL (masalan: https://api.absvision.uz)", False),
        ("username", "AbsVision foydalanuvchi", False),
        ("password", "AbsVision parol", True),
    ],
    "mysql_query": [
        ("db_host", "MySQL host (masalan: localhost)", False),
        ("db_user", "MySQL foydalanuvchi", False),
        ("db_password", "MySQL parol", True),
        ("db_name", "MySQL baza nomi", False),
    ],
}


def _collect_plugin_keys(name: str) -> dict:
    """Prompt for a plugin's config keys and return the plugin config dict."""
    keys = _PLUGIN_KEYS.get(name, [])
    cfg: dict = {}

    if keys:
        print(f"  {_cyan(name)} sozlamalari:")
        for key, hint, is_secret in keys:
            if is_secret:
                val = _prompt_secret(f"  {hint}")
            else:
                val = _prompt(f"  {hint}")
            if val:
                cfg[key] = val

    skipped = [k for k, _, _ in keys if k not in cfg]
    if skipped:
        print(f"  {_yellow('!')} Keyinroq config.json da to'ldiring: {', '.join(skipped)}")

    return {"name": name, "enabled": True, "config": cfg}


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
            print(_green(f"\u2713 @{username} ({name})"))
            break
        else:
            print(_red("\u2717 Invalid token. Check with @BotFather."))

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

        # Ollama: special handling — no API key, detect models
        if prov == "ollama":
            base_url = info.get("base_url", "http://localhost:11434/v1")
            ollama_api = base_url.replace("/v1", "")
            print("    Checking Ollama...", end=" ", flush=True)
            if _detect_ollama(ollama_api):
                print(_green("\u2713 Running"))
                models = _list_ollama_models(ollama_api)
                if models:
                    model_options = models
                else:
                    print(_yellow("    No models found. Pull one first: ollama pull qwen3.5:35b"))
                    model_options = [
                        ("qwen3.5:35b", "Qwen 3.5 35B \u2014 recommended"),
                        ("qwen3.5:9b", "Qwen 3.5 9B \u2014 lighter"),
                        ("llama3.3:70b", "Llama 3.3 70B"),
                    ]
            else:
                print(_yellow("! Not running"))
                print(_dim("    Install: curl -fsSL https://ollama.com/install.sh | sh"))
                model_options = [
                    ("qwen3.5:35b", "Qwen 3.5 35B \u2014 recommended"),
                    ("qwen3.5:9b", "Qwen 3.5 9B \u2014 lighter"),
                    ("llama3.3:70b", "Llama 3.3 70B"),
                ]

            # Custom Ollama URL?
            custom_url = _prompt("Ollama URL", base_url)
            base_url = custom_url

            selected_model = _prompt_select(
                "Which model?",
                model_options,
            )[0]

            if prov == primary_provider:
                primary_model = selected_model

            providers_config.append({
                "name": "ollama-main",
                "provider": "openai",  # Ollama is OpenAI-compatible
                "model": selected_model,
                "api_key": "ollama",  # Ollama doesn't need a real key
                "base_url": base_url,
            })
            continue

        # Cloud providers: ask for API key
        api_key = ""
        while True:
            api_key = _prompt_secret("API key", info["key_hint"])
            if not api_key:
                print(_red("    API key is required."))
                continue

            print("    Validating...", end=" ", flush=True)
            if _validate_api_key(prov, api_key):
                print(_green("\u2713 Valid"))
                break
            else:
                print(_red("\u2717 Invalid key."))
                if _prompt_yn("Try again?", default=True):
                    continue
                else:
                    print(_yellow("    Skipping validation, saving as-is."))
                    break

        # Ask for model
        model_options = info["models"]
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
        print(_green("  \u2713 Voice enabled (inbound mode \u2014 replies to voice with voice)"))

    # ── Step 4: Access Control ──
    print(f"\n{_bold('  Step 4: Access Control')}")
    print(f"  {_cyan('\u2139')} The first person to message the bot becomes the owner.")
    print(f"  {_dim('To restrict access later, add your Telegram user ID to allowed_users in config.json')}")
    allowed_users: list[int] = []
    owner_name = ""

    owner_name_input = _prompt("Your name (optional)", "")
    if owner_name_input:
        owner_name = owner_name_input

    # ── Step 5: Web Search (optional) ──
    print(f"\n{_bold('  Step 5: Web Search (optional)')}")
    brave_api_key = ""
    web_search_enabled = _prompt_yn("Enable web search? (free Brave Search API)")
    if web_search_enabled:
        brave_api_key = _prompt_secret("Brave Search API key", "Get free at brave.com/search/api")
        if brave_api_key:
            print(_green("  \u2713 Web search enabled"))
        else:
            print(_yellow("  ! Skipped \u2014 you can add brave_api_key to config.json later"))

    # ── Step 6: Integrations (plugins) ──
    print(f"\n{_bold('  Step 6: Integratsiyalar')}")
    print(f"  {_dim('Biznesingiz uchun kerakli integratsiyalarni tanlang.')}")

    plugins_config: list[dict] = []

    # POS / ERP — pick ONE
    pos_options = [
        ("none", "Kerak emas"),
        ("bito", "Bito POS — sotuvlar, tovarlar, mijozlar"),
        ("absmarket", "AbsMarket — 30 API tool + MySQL"),
        ("ibox", "ibox.io — ombor boshqaruv"),
        ("moysklad", "MoySklad — tovarlar, ombor, sotuvlar"),
        ("onec", "1C Enterprise — buxgalteriya"),
    ]
    selected_pos = _prompt_select("POS / ERP tizimi:", pos_options)[0]
    if selected_pos != "none":
        plugins_config.append(_collect_plugin_keys(selected_pos))

    # CRM — pick ONE
    crm_options = [
        ("none", "Kerak emas"),
        ("amocrm", "amoCRM — lidlar, kontaktlar, pipeline"),
        ("bitrix24", "Bitrix24 — deallar, vazifalar"),
    ]
    selected_crm = _prompt_select("CRM tizimi:", crm_options)[0]
    if selected_crm != "none":
        plugins_config.append(_collect_plugin_keys(selected_crm))

    # SMS
    if _prompt_yn("Eskiz SMS kerakmi?"):
        plugins_config.append(_collect_plugin_keys("eskiz"))

    # HR
    if _prompt_yn("AbsVision HR kerakmi?"):
        plugins_config.append(_collect_plugin_keys("absvision"))

    # MySQL
    if _prompt_yn("MySQL to'g'ridan-to'g'ri so'rov kerakmi?"):
        plugins_config.append(_collect_plugin_keys("mysql_query"))

    if plugins_config:
        print(f"  {_green(chr(10003))} Plaginlar: {', '.join(p['name'] for p in plugins_config)}")
    else:
        print(f"  {_dim('Plagin tanlanmadi. Keyinroq: qanot plugin install <name>')}")

    # ── Step 7: Build config ──
    print(f"\n{_bold('  Step 7: Saving configuration')}")

    config = {
        "bot_token": bot_token,
        "provider": primary_provider,
        "model": primary_model,
        "api_key": primary_api_key,
        "providers": providers_config,
        "owner_name": owner_name,
        "bot_name": bot_name,
        "timezone": "Asia/Tashkent",
        "max_concurrent": 4,
        "compaction_mode": "safeguard",
        "max_context_tokens": 200000,
        "allowed_users": allowed_users,
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
        "plugins": plugins_config,
    }

    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    config_path.chmod(0o600)

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

    print(_green(f"  \u2713 Config saved to {config_path}"))
    print(_green(f"  \u2713 Workspace created at {workspace}"))

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
    if plugins_config:
        print(f"  Plugins: {', '.join(p['name'] for p in plugins_config)}")
    print()

    # Auto-start after init (background)
    from qanot.cli.daemon_cmd import cmd_start
    cmd_start([str(target)])
