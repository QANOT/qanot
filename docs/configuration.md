# Configuration Reference

Qanot AI is configured through a single `config.json` file. This page documents every field.

## Config File Location

The config file is located by checking, in order:

1. Path passed to `qanot start <path>`
2. `QANOT_CONFIG` environment variable
3. `./config.json` in the current directory
4. `/data/config.json` (Docker default)

## Full Reference

### Core Settings

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `bot_token` | string | `""` | Telegram bot token from BotFather. Required. |
| `provider` | string | `"anthropic"` | LLM provider: `anthropic`, `openai`, `gemini`, `groq` |
| `model` | string | `"claude-sonnet-4-6"` | Model identifier for the chosen provider |
| `api_key` | string | `""` | API key for the provider |
| `owner_name` | string | `""` | Name of the bot owner (injected into system prompt) |
| `bot_name` | string | `""` | Display name of the bot (injected into system prompt) |
| `timezone` | string | `"Asia/Tashkent"` | IANA timezone for cron jobs and timestamps |

### Context and Compaction

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_context_tokens` | int | `200000` | Maximum context window size in tokens |
| `compaction_mode` | string | `"safeguard"` | Compaction strategy (currently only `safeguard`) |
| `max_concurrent` | int | `4` | Maximum concurrent message processing |

Context management thresholds (hardcoded, not configurable):

- **50%** -- Working Buffer activates, exchanges logged to `working-buffer.md`
- **60%** -- Proactive compaction triggers, middle messages removed from history
- **35%** -- Target context usage after compaction

### Telegram Settings

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `response_mode` | string | `"stream"` | How responses are delivered. See below. |
| `stream_flush_interval` | float | `0.8` | Seconds between streaming draft updates |
| `telegram_mode` | string | `"polling"` | Transport: `polling` or `webhook` |
| `webhook_url` | string | `""` | Public URL for webhook mode (e.g., `https://bot.example.com`) |
| `webhook_port` | int | `8443` | Local port for the webhook HTTP server |
| `allowed_users` | list[int] | `[]` | Telegram user IDs allowed to use the bot. Empty = allow all. |

**Response modes:**

| Mode | Mechanism | Behavior |
|------|-----------|----------|
| `stream` | `sendMessageDraft` (Bot API 9.5) | Real-time character streaming. Requires recent Telegram clients. |
| `partial` | `editMessageText` | Sends initial message, then edits with accumulated text at intervals. |
| `blocked` | `sendMessage` | Waits for the full response, then sends once. Simplest but slowest UX. |

### Directory Paths

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `workspace_dir` | string | `"/data/workspace"` | Agent workspace (SOUL.md, TOOLS.md, memory) |
| `sessions_dir` | string | `"/data/sessions"` | JSONL session log directory |
| `cron_dir` | string | `"/data/cron"` | Cron job definitions (jobs.json) |
| `plugins_dir` | string | `"/data/plugins"` | External plugins directory |

When using `qanot init`, these paths are set relative to the project directory instead of `/data/`.

### RAG Settings

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `rag_enabled` | bool | `true` | Enable RAG document indexing and search |
| `rag_mode` | string | `"auto"` | RAG retrieval strategy: `auto` (inject when relevant), `agentic` (agent decides via tools), `always` (inject on every turn) |

RAG requires a Gemini or OpenAI provider for embeddings. See [RAG documentation](rag.md) for details.

### Voice Settings

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `voice_provider` | string | `"muxlisa"` | Voice provider: `muxlisa`, `kotib`, `aisha`, `whisper` |
| `voice_api_key` | string | `""` | Default API key for the voice provider |
| `voice_api_keys` | dict | `{}` | Per-provider API keys: `{"muxlisa": "...", "kotib": "..."}` |
| `voice_mode` | string | `"inbound"` | Voice handling: `off` (disabled), `inbound` (STT only), `always` (STT + TTS) |
| `voice_name` | string | `""` | Voice name (e.g., `maftuna`/`asomiddin` for Muxlisa, `aziza`/`sherzod` for KotibAI) |
| `voice_language` | string | `""` | Force STT language (`uz`/`ru`/`en`). Empty = auto-detect. |

### Web Search

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `brave_api_key` | string | `""` | Brave Search API key (free tier: 2000 queries/month) |

### UX Settings

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `reactions_enabled` | bool | `false` | Send emoji reactions on messages |
| `reply_mode` | string | `"coalesced"` | Reply behavior: `off`, `coalesced`, `always` |

### Group Chat

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `group_mode` | string | `"mention"` | Group chat behavior: `off` (ignore groups), `mention` (respond to @bot and replies), `all` (respond to everything) |

### Heartbeat

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `heartbeat_enabled` | bool | `true` | Enable/disable heartbeat cron job |
| `heartbeat_interval` | string | `"0 */4 * * *"` | Cron expression for heartbeat schedule |

### Daily Briefing

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `briefing_enabled` | bool | `true` | Enable/disable daily morning briefing |
| `briefing_schedule` | string | `"0 8 * * *"` | Cron expression for briefing (default: 8:00 AM daily) |

### Memory and History

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_memory_injection_chars` | int | `4000` | Max characters for RAG/compaction injection into user messages |
| `history_limit` | int | `50` | Max user turns to restore from session history on restart |

### Extended Thinking

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `thinking_level` | string | `"off"` | Claude reasoning mode: `off`, `low`, `medium`, `high` |
| `thinking_budget` | int | `10000` | Maximum thinking tokens |

### Execution Security

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `exec_security` | string | `"cautious"` | Command execution security level: `open` (all commands), `cautious` (prompts for dangerous ops), `strict` (allowlist only) |
| `exec_allowlist` | list[string] | `[]` | In `strict` mode, only these commands are allowed |

### Dashboard

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `dashboard_enabled` | bool | `true` | Enable web dashboard |
| `dashboard_port` | int | `8765` | Port for the web dashboard |
| `dashboard_host` | string | `"127.0.0.1"` | Dashboard bind address (`0.0.0.0` for Docker, `127.0.0.1` for local) |

### Backup

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `backup_enabled` | bool | `true` | Enable automatic workspace backups on startup |

### Code Execution and Memory Tool

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `code_execution` | bool | `false` | Enable Anthropic server-side code execution (`code_execution_20250825`). Free with web search. |
| `memory_tool` | bool | `false` | Enable Anthropic memory tool (`memory_20250818`). Creates `/memories` directory for structured notes. |

### Browser

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `browser_enabled` | bool | `false` | Enable Playwright browser tools (browse_url, click, fill_form, screenshot, extract_data). Requires `pip install qanot[browser]`. |

### Webhook (External Events)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `webhook_enabled` | bool | `false` | Enable webhook endpoint for external events (GitHub, CRM, CI/CD) |
| `webhook_token` | string | `""` | Bearer token for webhook authentication |

### WebChat

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `webchat_enabled` | bool | `false` | Enable WebChat adapter with WebSocket streaming |
| `webchat_token` | string | `""` | Authentication token for webchat connections |
| `webchat_origins` | list[string] | `[]` | Allowed CORS origins for webchat (empty = allow all) |
| `webchat_max_sessions` | int | `50` | Maximum concurrent webchat sessions |

### MCP (Model Context Protocol)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mcp_servers` | list | `[]` | MCP server definitions. Requires `pip install qanot[mcp]`. |

Each MCP server entry:

```json
{
  "name": "server-name",
  "command": "npx",
  "args": ["-y", "@anthropic/mcp-server"],
  "env": {"API_KEY": "..."},
  "enabled": true
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Unique server identifier |
| `command` | string | required | Command to launch the MCP server |
| `args` | list[string] | `[]` | Command arguments |
| `env` | dict | `{}` | Environment variables for the server process |
| `enabled` | bool | `true` | Whether to connect on startup |

### Model Routing

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `routing_enabled` | bool | `false` | Enable 3-tier model routing for cost optimization |
| `routing_model` | string | `"claude-haiku-4-5-20251001"` | Cheap model for simple messages (greetings, acknowledgments) |
| `routing_mid_model` | string | `"claude-sonnet-4-6"` | Mid-tier model for general conversation |
| `routing_threshold` | float | `0.3` | Complexity score threshold (0.0--1.0) for routing decisions |

### Image Generation

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `image_api_key` | string | `""` | Dedicated Gemini API key for image generation (optional, uses provider key if empty) |
| `image_model` | string | `"gemini-3-pro-image-preview"` | Model for image generation and editing |

### Multi-Agent

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `agents` | list[AgentDefinition] | `[]` | Named agent definitions for delegation. See below. |
| `monitor_group_id` | int | `0` | Telegram group ID to mirror agent conversations for monitoring |

Each agent definition:

```json
{
  "id": "researcher",
  "name": "Tadqiqotchi",
  "prompt": "You are a research assistant...",
  "model": "",
  "provider": "",
  "api_key": "",
  "bot_token": "",
  "tools_allow": [],
  "tools_deny": [],
  "delegate_allow": [],
  "max_iterations": 15,
  "timeout": 120
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | string | required | Unique identifier (e.g., `researcher`, `coder`) |
| `name` | string | `""` | Human-readable name |
| `prompt` | string | `""` | System prompt / personality |
| `model` | string | `""` | Model override (empty = use main model) |
| `provider` | string | `""` | Provider override (empty = use main provider) |
| `api_key` | string | `""` | API key override (empty = use main) |
| `bot_token` | string | `""` | Separate Telegram bot token (empty = internal agent only) |
| `tools_allow` | list[string] | `[]` | Tool whitelist (empty = all tools) |
| `tools_deny` | list[string] | `[]` | Tool blacklist |
| `delegate_allow` | list[string] | `[]` | Which agents this one can delegate to (empty = all) |
| `max_iterations` | int | `15` | Max tool-use loops |
| `timeout` | int | `120` | Seconds before timeout |

### Plugin Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `plugins` | list | `[]` | Plugin configurations. See below. |

Each plugin entry:

```json
{
  "name": "myplugin",
  "enabled": true,
  "config": {
    "api_url": "https://example.com",
    "username": "admin"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Plugin directory name (looked up in `plugins/` built-in, then `plugins_dir`) |
| `enabled` | bool | Whether to load this plugin |
| `config` | dict | Arbitrary config passed to `plugin.setup(config)` |

### Multi-Provider Configuration

Instead of the single-provider fields (`provider`, `model`, `api_key`), you can configure multiple providers for automatic failover:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `providers` | list | `[]` | List of provider profiles. When set, enables failover mode. |

Each provider profile:

```json
{
  "name": "claude-main",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-...",
  "base_url": ""
}
```

See [Providers](providers.md) for failover details.

## Example Configurations

### Minimal (single provider, polling)

```json
{
  "bot_token": "123456:ABC-DEF...",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-..."
}
```

### Multi-Provider with Failover

```json
{
  "bot_token": "123456:ABC-DEF...",
  "providers": [
    {
      "name": "claude-main",
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "api_key": "sk-ant-..."
    },
    {
      "name": "gemini-backup",
      "provider": "gemini",
      "model": "gemini-2.5-flash",
      "api_key": "AIza..."
    },
    {
      "name": "groq-fast",
      "provider": "groq",
      "model": "llama-3.3-70b-versatile",
      "api_key": "gsk_..."
    }
  ],
  "owner_name": "Sardor",
  "bot_name": "Javis",
  "timezone": "Asia/Tashkent",
  "rag_enabled": true
}
```

### Production with Webhook

```json
{
  "bot_token": "123456:ABC-DEF...",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-...",
  "telegram_mode": "webhook",
  "webhook_url": "https://bot.example.com",
  "webhook_port": 8443,
  "response_mode": "stream",
  "allowed_users": [123456789, 987654321],
  "max_concurrent": 8
}
```

### Budget Setup (Groq, free tier)

```json
{
  "bot_token": "123456:ABC-DEF...",
  "provider": "groq",
  "model": "llama-3.3-70b-versatile",
  "api_key": "gsk_...",
  "response_mode": "partial",
  "rag_enabled": false,
  "max_context_tokens": 32000
}
```

Note: Groq does not support embeddings, so RAG requires a separate Gemini or OpenAI provider. With `rag_enabled: false`, the RAG tools are not registered.

### Local Development

When using `qanot init`, paths are set relative to the project directory:

```json
{
  "bot_token": "123456:ABC-DEF...",
  "provider": "openai",
  "model": "gpt-4.1",
  "api_key": "sk-...",
  "workspace_dir": "/home/user/mybot/workspace",
  "sessions_dir": "/home/user/mybot/sessions",
  "cron_dir": "/home/user/mybot/cron",
  "plugins_dir": "/home/user/mybot/plugins"
}
```
