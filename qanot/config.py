"""JSON config loader for Qanot AI."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PluginConfig:
    name: str
    enabled: bool = True
    config: dict = field(default_factory=dict)


@dataclass
class ProviderConfig:
    """Configuration for a single LLM provider profile."""
    name: str
    provider: str  # "anthropic", "openai", "gemini", "groq"
    model: str
    api_key: str
    base_url: str = ""


@dataclass
class Config:
    bot_token: str = ""
    # Legacy single-provider fields (still supported)
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""
    # Multi-provider support
    providers: list[ProviderConfig] = field(default_factory=list)
    # Paths
    soul_path: str = "/data/workspace/SOUL.md"
    tools_path: str = "/data/workspace/TOOLS.md"
    plugins: list[PluginConfig] = field(default_factory=list)
    owner_name: str = ""
    bot_name: str = ""
    timezone: str = "Asia/Tashkent"
    max_concurrent: int = 4
    compaction_mode: str = "safeguard"
    workspace_dir: str = "/data/workspace"
    sessions_dir: str = "/data/sessions"
    cron_dir: str = "/data/cron"
    plugins_dir: str = "/data/plugins"
    max_context_tokens: int = 200000
    allowed_users: list[int] = field(default_factory=list)
    response_mode: str = "stream"  # "stream" | "partial" | "blocked"
    stream_flush_interval: float = 0.8  # seconds between draft updates
    telegram_mode: str = "polling"  # "polling" | "webhook"
    webhook_url: str = ""  # e.g. "https://bot.example.com/webhook"
    webhook_port: int = 8443  # local port for webhook server
    # RAG
    rag_enabled: bool = True
    rag_mode: str = "auto"  # "auto" | "agentic" | "always"
    # Voice (Muxlisa.uz / KotibAI)
    voice_provider: str = "muxlisa"  # "muxlisa" | "kotib"
    voice_api_key: str = ""  # Default API key (used if per-provider key not set)
    voice_api_keys: dict[str, str] = field(default_factory=dict)  # Per-provider: {"muxlisa": "...", "kotib": "..."}
    voice_mode: str = "inbound"  # "off" | "inbound" | "always"
    voice_name: str = ""  # Voice name (maftuna/asomiddin for muxlisa, aziza/sherzod for kotib)
    voice_language: str = ""  # Force STT language (uz/ru/en), auto-detect if empty
    # Self-healing / heartbeat
    heartbeat_enabled: bool = True  # Enable/disable heartbeat cron
    heartbeat_interval: str = "0 */4 * * *"  # Cron expression for heartbeat schedule

    def get_voice_api_key(self, provider: str | None = None) -> str:
        """Get API key for the given voice provider, with fallback to default."""
        p = provider or self.voice_provider
        return self.voice_api_keys.get(p, self.voice_api_key)


def load_config(path: str | None = None) -> Config:
    """Load configuration from JSON file."""
    if path is None:
        import os
        path = os.environ.get("QANOT_CONFIG", "/data/config.json")
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw = json.loads(p.read_text(encoding="utf-8"))

    plugins = []
    for pl in raw.get("plugins", []):
        if isinstance(pl, str):
            plugins.append(PluginConfig(name=pl))
        elif isinstance(pl, dict):
            plugins.append(PluginConfig(
                name=pl["name"],
                enabled=pl.get("enabled", True),
                config=pl.get("config", {}),
            ))

    # Parse multi-provider configs
    provider_configs = []
    for pc in raw.get("providers", []):
        provider_configs.append(ProviderConfig(
            name=pc.get("name", pc.get("provider", "default")),
            provider=pc["provider"],
            model=pc["model"],
            api_key=pc["api_key"],
            base_url=pc.get("base_url", ""),
        ))

    # Auto-map simple fields (str, int, float, bool, list, dict) from JSON to dataclass.
    # Only nested types (plugins, providers) need manual parsing.
    import dataclasses
    _NESTED_FIELDS = {"plugins", "providers"}
    simple = {}
    for f in dataclasses.fields(Config):
        if f.name in _NESTED_FIELDS:
            continue
        if f.name in raw:
            simple[f.name] = raw[f.name]

    return Config(
        **simple,
        plugins=plugins,
        providers=provider_configs,
    )
