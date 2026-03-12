# Qanot AI

Lightweight Python agent framework for Telegram bots. Built on top of Claude and GPT with tool-use loops, persistent memory, and a plugin system.

PyPI: [`qanot`](https://pypi.org/project/qanot/) | License: MIT

## Features

- **Agent loop** — tool_use cycle with up to 25 iterations per turn
- **Multi-provider** — Anthropic (Claude), OpenAI (GPT), Gemini, Groq with failover
- **3-tier memory** — WAL protocol (SESSION-STATE.md), daily notes, and long-term MEMORY.md
- **Working Buffer** — automatic context management at 60% token usage
- **Self-healing** — autonomous heartbeat checks workspace integrity, fixes issues, reports to monitoring group
- **Image understanding** — vision model support with auto-downscaling (max 1200px) and context bloat prevention
- **Voice I/O** — STT/TTS via Muxlisa and KotibAI providers with per-provider API keys
- **RAG** — semantic memory search with sqlite-vec embeddings
- **Streaming** — native Telegram sendMessageDraft (Bot API 9.5) with partial edit fallback
- **Reactions** — 👀 processing, ✅ done, ❌ error emoji feedback
- **Plugin system** — auto-discovery, hot-loadable plugins with `@tool` decorator
- **Cron scheduler** — APScheduler-based with isolated agent and system event modes
- **Telegram adapter** — aiogram 3.x with streaming, webhook support, per-user isolation
- **JSONL sessions** — append-only audit trail with file locking
- **CLI** — interactive `qanot init` wizard, `qanot start`, `qanot version`

## Quick Start

```bash
pip install qanot
```

### 1. Initialize a project

```bash
qanot init mybot
```

Interactive wizard walks through:
- Telegram bot token (validates via getMe API)
- AI provider selection (Anthropic, OpenAI, Gemini, Groq)
- API key validation (test call to provider)
- Voice provider setup (Muxlisa, KotibAI)
- User access control

### 2. Start

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

## Self-Healing

Qanot includes an autonomous self-healing system that runs on a configurable schedule (default: every 4 hours):

- **Pending tasks** — checks daily notes for uncompleted tasks and follow-ups
- **Workspace integrity** — verifies critical files exist and aren't corrupted
- **Memory consolidation** — distills old daily notes into MEMORY.md
- **TOOLS.md validation** — scans for incorrect examples or stale references
- **Pattern detection** — identifies repeated user requests for automation
- **Idle-aware** — skips heartbeat when user is actively chatting (saves tokens)
- **HEARTBEAT_OK suppression** — silent when nothing needs attention

Reports are delivered to the owner (first `allowed_users` entry).

## Plugins

Place plugins in the configured `plugins_dir` (default: `/data/plugins/`):

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
    → Reaction 👀 (acknowledge)
    → WAL scan (write-ahead to SESSION-STATE.md)
    → Image download + downscale (if photo)
    → Voice transcribe (if voice/video note)
    → Agent loop (max 25 iterations)
        → Build system prompt (SOUL + IDENTITY + SKILL + TOOLS + AGENTS + SESSION-STATE + USER)
        → LLM call (Anthropic, OpenAI, Gemini, or Groq)
        → If tool_use: execute → loop
        → If end_turn: log + daily note → respond
    → Reaction ✅ (success) or ❌ (error)
    → TTS voice reply (if voice mode enabled)

Heartbeat (every 4h) → Isolated Agent
    → Read HEARTBEAT.md checklist
    → Check workspace: pending tasks, integrity, memory
    → Fix issues silently
    → Report to monitoring group (or HEARTBEAT_OK if clean)
```

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `bot_token` | — | Telegram bot token |
| `provider` | `anthropic` | LLM provider |
| `model` | `claude-sonnet-4-6` | Model identifier |
| `api_key` | — | Provider API key |
| `providers` | `[]` | Multi-provider failover config |
| `owner_name` | — | Human owner name (injected into prompts) |
| `bot_name` | — | Agent name (injected into prompts) |
| `timezone` | `Asia/Tashkent` | Scheduler timezone |
| `max_concurrent` | `4` | Max concurrent Telegram users |
| `max_context_tokens` | `200000` | Context window limit |
| `allowed_users` | `[]` | Telegram user IDs (empty = public) |
| `response_mode` | `stream` | `stream` / `partial` / `blocked` |
| `stream_flush_interval` | `0.8` | Seconds between draft updates |
| `telegram_mode` | `polling` | `polling` / `webhook` |
| `rag_enabled` | `true` | Enable RAG semantic search |
| `voice_provider` | `muxlisa` | `muxlisa` / `kotib` |
| `voice_mode` | `inbound` | `off` / `inbound` / `always` |
| `voice_api_keys` | `{}` | Per-provider voice API keys |
| `heartbeat_enabled` | `true` | Enable self-healing heartbeat |
| `heartbeat_interval` | `0 */4 * * *` | Heartbeat cron schedule |

## License

MIT
