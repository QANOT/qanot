"""JSON config loader for Qanot AI."""

from __future__ import annotations

import json
import re
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import os

_CONTROL_CHAR_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def get_config_path() -> str:
    """Get config file path from QANOT_CONFIG env or default."""
    return os.environ.get("QANOT_CONFIG", "/data/config.json")


def read_config_json(config_path: str | Path | None = None) -> dict:
    """Read config JSON from disk. Uses QANOT_CONFIG env default."""
    p = Path(config_path or get_config_path())
    return json.loads(p.read_text(encoding="utf-8"))


def write_config_json(data: dict, config_path: str | Path | None = None) -> None:
    """Write config JSON atomically. Uses QANOT_CONFIG env default."""
    from qanot.utils import atomic_write
    p = Path(config_path or get_config_path())
    atomic_write(p, json.dumps(data, indent=2, ensure_ascii=False))


@dataclass
class PluginConfig:
    """Configuration for a single plugin entry."""

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
class AgentDefinition:
    """Configuration for a named agent that can be delegated to."""
    id: str  # Unique identifier (e.g., "researcher", "coder", "my-analyst")
    name: str = ""  # Human-readable name (e.g., "Tadqiqotchi")
    prompt: str = ""  # System prompt / personality
    model: str = ""  # Model override (empty = use main model)
    provider: str = ""  # Provider override (empty = use main provider)
    api_key: str = ""  # API key override (empty = use main)
    bot_token: str = ""  # Separate Telegram bot token (empty = internal agent only)
    tools_allow: list[str] = field(default_factory=list)  # Whitelist (empty = all)
    tools_deny: list[str] = field(default_factory=list)  # Blacklist
    delegate_allow: list[str] = field(default_factory=list)  # Which agents this one can delegate to (empty = all)
    max_iterations: int = 15  # Max tool-use loops for this agent
    timeout: int = 120  # Seconds before timeout


@dataclass
class Config:
    """Top-level configuration for a Qanot AI bot instance."""

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
    max_context_tokens: int = 0  # 0 = auto-detect from model (1M for Opus/Sonnet 4.6, 200K otherwise)
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
    # Web search
    brave_api_key: str = ""  # Brave Search API key (free tier: 2000/month)
    # Cost budget (0 = unlimited)
    daily_budget_usd: float = 0.0  # Max daily spend in USD per user (0 = no limit)
    budget_warning_pct: int = 80  # Warn user when reaching this % of daily budget
    # UX
    reactions_enabled: bool = False  # Send emoji reactions (👀, ✅, ❌) on messages
    reply_mode: str = "coalesced"  # "off" | "coalesced" | "always"
    # Group chat
    group_mode: str = "mention"  # "off" | "mention" | "all"
    # Topic-agent bindings: "chat_id:topic_id" → agent_id
    # Allows binding specific agents to specific forum topics in groups
    topic_bindings: dict[str, str] = field(default_factory=dict)
    # Self-healing / heartbeat
    heartbeat_enabled: bool = True  # Enable/disable heartbeat cron
    heartbeat_interval: str = "0 */4 * * *"  # Cron expression for heartbeat schedule
    # Daily briefing
    briefing_enabled: bool = True  # Enable/disable daily morning briefing
    briefing_schedule: str = "0 8 * * *"  # Default: 8:00 AM daily
    # Memory injection budget
    max_memory_injection_chars: int = 4000  # Max chars for RAG/compaction injection into user message
    # Session history replay
    history_limit: int = 50  # Max user turns to restore from session history on restart
    # Extended thinking (Claude reasoning mode)
    thinking_level: str = "off"  # "off" | "minimal" | "low" | "medium" | "high" | "extended" | "max"
    thinking_budget: int = 10000  # max thinking tokens (auto-set by level)
    # Anthropic server-side code execution (free with web search)
    code_execution: bool = False  # Enable Claude's sandboxed code execution
    # Anthropic memory tool (persistent /memories directory)
    memory_tool: bool = True  # Enable memory tool for all providers (Anthropic gets trained behavior)
    # Execution security
    exec_security: str = "cautious"  # "open" | "cautious" | "strict"
    exec_allowlist: list[str] = field(default_factory=list)  # strict mode: only these commands allowed
    # Dashboard
    dashboard_enabled: bool = True  # Enable web dashboard
    dashboard_port: int = 8765  # Dashboard port
    dashboard_host: str = "127.0.0.1"  # Bind address (use 0.0.0.0 for Docker)
    dashboard_token: str = ""  # Auth token for dashboard API (empty = no auth)
    # Backup
    backup_enabled: bool = True  # Enable startup backups
    # Model routing (cost optimization)
    routing_enabled: bool = False  # Route simple messages to cheaper model
    routing_model: str = "claude-haiku-4-5-20251001"  # Cheap model (greetings)
    routing_mid_model: str = "claude-sonnet-4-6"  # Mid model (general conversation)
    routing_threshold: float = 0.3  # Complexity score threshold (0.0-1.0)
    # Image generation (Nano Banana / Gemini)
    image_api_key: str = ""  # Dedicated Gemini key for images (optional, uses provider key if empty)
    image_model: str = "gemini-3-pro-image-preview"  # Nano Banana Pro (highest quality)
    # Multi-agent definitions
    agents: list[AgentDefinition] = field(default_factory=list)
    # Agent monitoring — mirror agent conversations to this Telegram group
    monitor_group_id: int = 0
    # Multi-agent orchestrator (spawn_agent, list_agents, etc.)
    agents_enabled: bool = False
    # MCP (Model Context Protocol) servers
    mcp_servers: list[dict] = field(default_factory=list)
    # Browser control (Playwright)
    browser_enabled: bool = False
    # Webhook endpoint
    webhook_enabled: bool = False
    webhook_token: str = ""
    # WebChat (embeddable chat widget)
    webchat_enabled: bool = False
    webchat_token: str = ""
    webchat_origins: list[str] = field(default_factory=list)
    webchat_max_sessions: int = 50
    # Voice Call (Telegram voice chat via py-tgcalls, requires userbot)
    voicecall_enabled: bool = False  # Master switch (disabled by default)
    voicecall_api_id: int = 0  # Telegram API ID (from my.telegram.org)
    voicecall_api_hash: str = ""  # Telegram API hash
    voicecall_session: str = ""  # Pyrogram session string (base64, from initial auth)
    voicecall_vad_threshold: float = 0.5  # Silero VAD speech probability threshold
    voicecall_silence_ms: int = 400  # Silence duration (ms) to end speech segment
    voicecall_min_speech_ms: int = 250  # Min speech duration (ms) to process (filters coughs)
    voicecall_max_calls: int = 3  # Max simultaneous voice calls
    voicecall_auto_leave_minutes: int = 30  # Auto-leave after N minutes of no speech
    voicecall_barge_in: bool = True  # Allow user to interrupt bot speech

    def get_voice_api_key(self, provider: str | None = None) -> str:
        """Get API key for the given voice provider, with fallback to default."""
        return self.voice_api_keys.get(provider or self.voice_provider, self.voice_api_key)


def load_config(path: str | None = None) -> Config:
    """Load configuration from a JSON file and return a Config dataclass.

    Args:
        path: Path to config JSON. Falls back to ``$QANOT_CONFIG``
            env var, then ``/data/config.json``.

    Returns:
        Fully parsed Config with resolved secrets, validated fields,
        and nested ProviderConfig / AgentDefinition / PluginConfig lists.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If required fields are missing or values are invalid.
    """
    if path is None:
        path = get_config_path()
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw = json.loads(p.read_text(encoding="utf-8"))

    # Resolve SecretRef values (env vars, files) before parsing config
    from qanot.secrets import resolve_config_secrets
    raw = resolve_config_secrets(raw)

    plugins = []
    for i, pl in enumerate(raw.get("plugins", [])):
        if isinstance(pl, str):
            plugins.append(PluginConfig(name=pl))
        elif isinstance(pl, dict):
            if "name" not in pl:
                raise ValueError(
                    f"Plugin at index {i} is missing required 'name' field"
                )
            plugins.append(PluginConfig(
                name=pl["name"],
                enabled=pl.get("enabled", True),
                config=pl.get("config", {}),
            ))
        else:
            raise TypeError(
                f"Plugin at index {i} must be a string or dict, got {type(pl).__name__}"
            )

    def _dict_to_dataclass(cls, data: dict):
        """Map a dict to a dataclass using field names and defaults."""
        if not isinstance(data, dict):
            raise TypeError(
                f"Expected dict for {cls.__name__}, got {type(data).__name__}"
            )
        kwargs = {}
        missing = []
        for f in dataclasses.fields(cls):
            if f.name in data:
                kwargs[f.name] = data[f.name]
            elif f.default is not dataclasses.MISSING:
                kwargs[f.name] = f.default
            elif f.default_factory is not dataclasses.MISSING:
                kwargs[f.name] = f.default_factory()
            else:
                missing.append(f.name)
        if missing:
            raise ValueError(
                f"{cls.__name__} is missing required field(s): {', '.join(missing)}"
            )
        return cls(**kwargs)

    # Parse multi-provider configs
    provider_configs = []
    for pc in raw.get("providers", []):
        # Special case: "name" falls back to provider name
        if "name" not in pc:
            pc = {**pc, "name": pc.get("provider", "default")}
        provider_configs.append(_dict_to_dataclass(ProviderConfig, pc))

    # Parse agent definitions
    agent_defs = [
        _dict_to_dataclass(AgentDefinition, ad)
        for ad in raw.get("agents", [])
    ]

    # Auto-map simple fields (str, int, float, bool, list, dict) from JSON to dataclass.
    # Only nested types (plugins, providers, agents) need manual parsing.
    _NESTED_FIELDS = {"plugins", "providers", "agents"}
    simple = {}
    for f in dataclasses.fields(Config):
        if f.name in _NESTED_FIELDS:
            continue
        if f.name in raw:
            simple[f.name] = raw[f.name]

    # Sanitize string fields: reject control characters (null bytes, newlines, etc.)
    # that could enable injection attacks in HTTP headers, file paths, or API calls
    _SENSITIVE_FIELDS = {
        'bot_token', 'api_key', 'brave_api_key', 'voice_api_key',
        'image_api_key', 'webhook_url', 'base_url',
        'soul_path', 'tools_path', 'workspace_dir', 'sessions_dir',
        'cron_dir', 'plugins_dir',
        'voicecall_api_hash', 'voicecall_session',
    }
    for key, value in simple.items():
        if isinstance(value, str) and key in _SENSITIVE_FIELDS:
            if _CONTROL_CHAR_RE.search(value):
                raise ValueError(
                    f"Config field '{key}' contains invalid control characters"
                )
        elif key == 'voice_api_keys' and isinstance(value, dict):
            for provider_name, api_key in value.items():
                if isinstance(api_key, str) and _CONTROL_CHAR_RE.search(api_key):
                    raise ValueError(
                        f"Config field 'voice_api_keys[\"{provider_name}\"]' contains invalid control characters"
                    )
    # Validate numeric fields with security-relevant bounds
    _INT_BOUNDS = [
        ('webhook_port', 1, 65535),
        ('max_concurrent', 1, 100),
        ('history_limit', 0, 10000),
    ]
    for _field, _lo, _hi in _INT_BOUNDS:
        if _field in simple:
            _v = simple[_field]
            if not isinstance(_v, int) or _v < _lo or _v > _hi:
                raise ValueError(
                    f"Config field '{_field}' must be an integer between {_lo} and {_hi}, got {_v!r}"
                )

    # Also validate provider and agent secrets
    for pc in provider_configs:
        for attr in ('api_key', 'base_url'):
            val = getattr(pc, attr, '')
            if val and _CONTROL_CHAR_RE.search(val):
                raise ValueError(
                    f"Provider '{pc.name}' field '{attr}' contains invalid control characters"
                )
    for ad in agent_defs:
        for attr in ('api_key', 'bot_token'):
            val = getattr(ad, attr, '')
            if val and _CONTROL_CHAR_RE.search(val):
                raise ValueError(
                    f"Agent '{ad.id}' field '{attr}' contains invalid control characters"
                )

    return Config(
        **simple,
        plugins=plugins,
        providers=provider_configs,
        agents=agent_defs,
    )
