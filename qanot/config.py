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

    return Config(
        bot_token=raw.get("bot_token", ""),
        provider=raw.get("provider", "anthropic"),
        model=raw.get("model", "claude-sonnet-4-6"),
        api_key=raw.get("api_key", ""),
        providers=provider_configs,
        soul_path=raw.get("soul_path", "/data/workspace/SOUL.md"),
        tools_path=raw.get("tools_path", "/data/workspace/TOOLS.md"),
        plugins=plugins,
        owner_name=raw.get("owner_name", ""),
        bot_name=raw.get("bot_name", ""),
        timezone=raw.get("timezone", "Asia/Tashkent"),
        max_concurrent=raw.get("max_concurrent", 4),
        compaction_mode=raw.get("compaction_mode", "safeguard"),
        workspace_dir=raw.get("workspace_dir", "/data/workspace"),
        sessions_dir=raw.get("sessions_dir", "/data/sessions"),
        cron_dir=raw.get("cron_dir", "/data/cron"),
        plugins_dir=raw.get("plugins_dir", "/data/plugins"),
        max_context_tokens=raw.get("max_context_tokens", 200000),
        allowed_users=raw.get("allowed_users", []),
        response_mode=raw.get("response_mode", "stream"),
        stream_flush_interval=raw.get("stream_flush_interval", 0.8),
        telegram_mode=raw.get("telegram_mode", "polling"),
        webhook_url=raw.get("webhook_url", ""),
        webhook_port=raw.get("webhook_port", 8443),
        rag_enabled=raw.get("rag_enabled", True),
        rag_mode=raw.get("rag_mode", "auto"),
    )
