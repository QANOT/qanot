# Getting Started

This guide walks you through installing Qanot AI, creating your first bot, and sending your first message.

## Prerequisites

- Python 3.11 or higher
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An API key from at least one LLM provider (Anthropic, OpenAI, Google Gemini, or Groq)

## Installation

Install from PyPI:

```bash
pip install qanot
```

For RAG support (document indexing and semantic search), install with the optional dependency:

```bash
pip install qanot[rag]
```

This installs `sqlite-vec`, the SQLite extension used for vector storage.

## Creating a Project

Use the CLI to scaffold a new project:

```bash
qanot init mybot
```

This creates a `mybot/` directory with a `config.json` file. The directory structure after first run looks like this:

```
mybot/
├── config.json          # Your configuration
├── workspace/           # Agent workspace (created on first run)
│   ├── SOUL.md          # Agent personality and instructions
│   ├── TOOLS.md         # Tool documentation for the agent
│   ├── IDENTITY.md      # Agent name and style
│   ├── SKILL.md         # Proactive behaviors
│   ├── AGENTS.md        # Operating rules
│   ├── MEMORY.md        # Long-term memory
│   ├── SESSION-STATE.md # Active session state (WAL entries)
│   └── memory/          # Daily notes directory
├── sessions/            # JSONL session logs
├── cron/                # Cron job definitions
└── plugins/             # Custom plugin directory
```

## Configuration

Open `config.json` and fill in the required fields:

```json
{
  "bot_token": "123456:ABC-DEF...",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-...",
  "owner_name": "Sardor",
  "bot_name": "MyAssistant",
  "timezone": "Asia/Tashkent"
}
```

At minimum, you need:

| Field | Description |
|-------|-------------|
| `bot_token` | Telegram bot token from BotFather |
| `provider` | LLM provider: `anthropic`, `openai`, `gemini`, or `groq` |
| `model` | Model name (e.g., `claude-sonnet-4-6`, `gpt-4.1`, `gemini-2.5-flash`) |
| `api_key` | API key for your chosen provider |

See the [Configuration Reference](configuration.md) for all available fields.

## Running the Bot

Start the bot:

```bash
qanot start mybot
```

You should see output like:

```
  ___                    _
 / _ \  __ _ _ __   ___ | |_
| | | |/ _` | '_ \ / _ \| __|
| |_| | (_| | | | | (_) | |_
 \__\_\\__,_|_| |_|\___/ \__|

Config: mybot/config.json

2025-01-15 10:00:00 [qanot] INFO: Config loaded: provider=anthropic, model=claude-sonnet-4-6
2025-01-15 10:00:00 [qanot] INFO: Provider initialized: anthropic
2025-01-15 10:00:00 [qanot] INFO: Tools registered: read_file, write_file, list_files, run_command, memory_search, session_status, cost_status, send_file, doctor, cron_create, cron_list, cron_delete, cron_update, web_search, web_fetch, generate_image, edit_image, rag_search, rag_index, rag_list, rag_forget
2025-01-15 10:00:00 [qanot] INFO: Cron scheduler started with 1 jobs
2025-01-15 10:00:01 [qanot.telegram] INFO: [telegram] starting — transport=polling, response=stream, flush=0.8s
```

Open Telegram, find your bot, and send a message. The bot responds with streaming text.

## Alternative Run Methods

**Using environment variable:**

```bash
export QANOT_CONFIG=/path/to/config.json
qanot start
```

**Running as a Python module:**

```bash
QANOT_CONFIG=mybot/config.json python3 -m qanot
```

**Docker (typical production setup):**

```dockerfile
FROM python:3.11-slim
RUN pip install qanot[rag]
COPY config.json /data/config.json
CMD ["qanot", "start"]
```

When running in Docker, the default paths (`/data/workspace`, `/data/sessions`, etc.) work without modification.

## First Interaction

Send your bot a message on Telegram. Here is what happens under the hood:

1. The Telegram adapter receives the message
2. The WAL protocol scans for corrections, preferences, and decisions
3. The agent builds a system prompt from workspace files
4. The LLM generates a response, potentially using tools
5. The response streams back to Telegram in real time
6. The exchange is logged to daily notes and session files

The bot can read and write files in its workspace, search the web, run sandboxed commands, and manage cron jobs -- all through natural conversation.

## Customizing Your Bot

Edit the workspace files to shape your bot's behavior:

- **`workspace/SOUL.md`** -- Core personality, instructions, and behavioral guidelines
- **`workspace/IDENTITY.md`** -- Name, communication style, emoji preferences
- **`workspace/SKILL.md`** -- Proactive behaviors and self-improvement patterns
- **`workspace/TOOLS.md`** -- Tool usage documentation for the agent

These files are included in the system prompt on every turn. Changes take effect immediately.

## Next Steps

- [Configuration Reference](configuration.md) -- full config field reference
- [LLM Providers](providers.md) -- set up multiple providers with failover
- [Tools](tools.md) -- available tools and how to create custom ones
- [Memory System](memory.md) -- how the bot remembers across conversations
