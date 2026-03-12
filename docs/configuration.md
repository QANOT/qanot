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

- **60%** -- Working Buffer activates, exchanges logged to `working-buffer.md`
- **70%** -- Proactive compaction triggers, middle messages removed from history
- **40%** -- Target context usage after compaction

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

RAG requires a Gemini or OpenAI provider for embeddings. See [RAG documentation](rag.md) for details.

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
