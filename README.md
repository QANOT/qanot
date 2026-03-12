# Qanot AI

Lightweight Python agent framework for Telegram bots. Built on top of Claude and GPT with tool-use loops, persistent memory, and a plugin system.

## Features

- **Agent loop** — tool_use cycle with up to 25 iterations per turn
- **Multi-provider** — Anthropic (Claude) and OpenAI (GPT) with a unified interface
- **3-tier memory** — WAL protocol (SESSION-STATE.md), daily notes, and long-term MEMORY.md
- **Working Buffer** — automatic context management at 60% token usage
- **Plugin system** — auto-discovery, hot-loadable plugins with `@tool` decorator
- **Cron scheduler** — APScheduler-based with isolated agent and system event modes
- **Telegram adapter** — aiogram 3.x with file uploads, markdown rendering, per-user isolation
- **JSONL sessions** — append-only audit trail with file locking
- **CLI** — `qanot init`, `qanot start`, `qanot version`

## Quick Start

```bash
pip install qanot
```

### 1. Initialize a project

```bash
qanot init mybot
```

This creates `mybot/config.json` with default settings.

### 2. Configure

Edit `mybot/config.json`:

```json
{
  "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
  "provider": "anthropic",
  "model": "claude-sonnet-4-5-20250514",
  "api_key": "YOUR_API_KEY",
  "owner_name": "Your Name",
  "bot_name": "My Bot"
}
```

Supported providers:
- `anthropic` — Claude (Sonnet, Haiku, Opus)
- `openai` — GPT (4.1, 4o, 4o-mini)

### 3. Start

```bash
qanot start mybot
```

Or with an environment variable:

```bash
QANOT_CONFIG=/path/to/config.json qanot start
```

## Docker

```bash
docker build -t qanot .
docker run -v /path/to/data:/data qanot
```

Mount `/data` with your `config.json`, and the framework will create `workspace/`, `sessions/`, `cron/`, and `plugins/` directories automatically.

## Plugins

Place plugins in the configured `plugins_dir` (default: `/data/plugins/`). Each plugin needs a `plugin.py` with a `QanotPlugin` class extending `qanot.plugins.base.Plugin`:

```python
from qanot.plugins.base import Plugin, tool

class QanotPlugin(Plugin):
    name = "my_plugin"

    @tool("Describe what this tool does")
    async def my_tool(self, params: dict) -> str:
        return '{"result": "done"}'
```

## Architecture

```
User Message → Telegram Adapter
    → WAL scan (write-ahead to SESSION-STATE.md)
    → Agent loop (max 25 iterations)
        → Build system prompt (SOUL + SKILL + TOOLS + SESSION-STATE + USER)
        → LLM call (Anthropic or OpenAI)
        → If tool_use: execute → loop
        → If end_turn: log + daily note → respond
```

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `bot_token` | — | Telegram bot token |
| `provider` | `anthropic` | LLM provider (`anthropic` or `openai`) |
| `model` | `claude-sonnet-4-5-20250514` | Model identifier |
| `api_key` | — | Provider API key |
| `owner_name` | — | Human owner name (injected into prompts) |
| `bot_name` | — | Agent name (injected into prompts) |
| `timezone` | `Asia/Tashkent` | Scheduler timezone |
| `max_concurrent` | `4` | Max concurrent Telegram users |
| `max_context_tokens` | `200000` | Context window limit |
| `allowed_users` | `[]` | Telegram user IDs (empty = public) |
| `plugins` | `[]` | Plugin configurations |

## License

MIT
