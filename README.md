<p align="center">
  <h1 align="center">Qanot AI</h1>
  <p align="center">
    <strong>Build intelligent Telegram agents in minutes, not months.</strong>
  </p>
  <p align="center">
    <a href="https://pypi.org/project/qanot/"><img src="https://img.shields.io/pypi/v/qanot?color=blue&label=PyPI" alt="PyPI"></a>
    <a href="https://pypi.org/project/qanot/"><img src="https://img.shields.io/pypi/pyversions/qanot" alt="Python"></a>
    <a href="https://github.com/sirli-ai/qanotai/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
    <a href="https://github.com/sirli-ai/qanotai/stargazers"><img src="https://img.shields.io/github/stars/sirli-ai/qanotai?style=social" alt="Stars"></a>
  </p>
</p>

> **Qanot** (Uzbek for "wing") is a lightweight Python framework for building AI-powered Telegram bots with agent loops, multi-agent delegation, persistent memory, RAG, image generation, and multi-provider failover — all out of the box.

---

## Why Qanot?

Most agent frameworks give you building blocks and say "good luck." Qanot gives you a **flying agent** in 3 commands:

```bash
pip install qanot
qanot init mybot
qanot start mybot
```

That's it. Your bot is live on Telegram with tool-use, memory, streaming, and multi-agent delegation.

---

## Highlights

- **Agent loop** — up to 25 tool-use iterations per turn, autonomous thinking-acting-observing cycle
- **Multi-agent system** — delegate tasks between agents, real-time monitoring, loop detection
- **Multi-provider failover** — Claude, GPT, Gemini, Groq with automatic switchover and smart cooldowns
- **3-tier memory** — WAL protocol (real-time capture), daily notes, long-term knowledge
- **Built-in RAG** — hybrid search (70% semantic + 30% keyword) with sqlite-vec
- **Image generation** — Gemini-powered image creation with workspace storage
- **Model routing** — automatic complexity detection, routes simple queries to cheaper models
- **Extended thinking** — Anthropic thinking mode with configurable budget
- **Voice I/O** — STT/TTS via Muxlisa & KotibAI with per-provider API keys
- **Streaming** — native Telegram `sendMessageDraft` (Bot API 9.5) with partial edit fallback
- **Web search** — Brave API integration for real-time information
- **Plugin system** — auto-discovery, hot-loadable plugins with `@tool` decorator
- **Self-healing** — autonomous heartbeat checks workspace integrity, fixes issues silently
- **Group chat** — mention-gated responses, per-group isolation, multi-bot coordination
- **Cron scheduler** — APScheduler-based with isolated agent and system event modes
- **Doctor diagnostics** — built-in health checker for config, providers, memory, sessions
- **Backup system** — automatic workspace snapshots with rotation
- **Prompt caching** — Anthropic cache headers for reduced latency and cost

---

## Multi-Agent System

Qanot's standout feature: agents that talk to each other.

```
User → Main Agent
         ├── delegate_to_agent("researcher", "find market data")
         ├── converse_with_agent("advisor", "review this plan", rounds=3)
         └── spawn_sub_agent("writer", "draft the report")

Monitor Group (Telegram)
  📡 Real-time feed of all agent conversations
  🤖 Each agent posts as its own bot (natural chat look)
  🔄 Loop detection prevents A→B→A→B ping-pong
```

### Agent Configuration

```json
{
  "agents": [
    {
      "id": "researcher",
      "name": "Research Agent",
      "prompt": "You research topics thoroughly",
      "bot_token": "123:ABC",
      "model": "claude-sonnet-4-6",
      "tools_allow": ["web_search", "memory_read"]
    },
    {
      "id": "advisor",
      "name": "Business Advisor",
      "prompt": "You provide strategic business advice"
    }
  ],
  "monitor_group_id": -1001234567890
}
```

### Delegation Tools

| Tool | What it does |
|------|-------------|
| `delegate_to_agent` | Send a one-shot task to another agent |
| `converse_with_agent` | Multi-turn conversation between agents |
| `spawn_sub_agent` | Fire-and-forget background task |
| `view_agent_activity` | Real-time activity log |
| `create_agent` | Create new agents at runtime |
| `update_agent` / `delete_agent` | Manage agents dynamically |

---

## What You Get

### Agent Loop
Up to 25 tool-use iterations per turn. Your agent thinks, acts, observes, and repeats — autonomously.

### Multi-Provider Failover
Claude, GPT, Gemini, Groq. If one goes down, Qanot switches automatically with smart cooldowns. Zero downtime.

```json
{
  "providers": [
    { "name": "claude-main", "provider": "anthropic", "model": "claude-sonnet-4-6" },
    { "name": "gemini-backup", "provider": "gemini", "model": "gemini-2.5-flash" }
  ]
}
```

### 3-Tier Memory

| Tier | What it does |
|------|-------------|
| **WAL** | Captures corrections, preferences, decisions in real-time |
| **Daily Notes** | Summarizes each day's context automatically |
| **Long-term** | Distilled knowledge that persists forever |

### Model Routing
Automatically routes simple queries (greetings, short questions) to cheaper models and complex tasks to powerful ones. Saves 40-60% on API costs.

```json
{
  "routing_enabled": true,
  "routing_model": "gemini-2.5-flash",
  "routing_threshold": 0.3
}
```

### Image Generation
Generate images via Gemini with a single tool call. Images are saved to workspace and delivered to the user.

### Built-in RAG
Hybrid search (70% semantic + 30% keyword) with sqlite-vec. Your agent remembers documents, not just conversations.

### Voice I/O
Speech-to-text and text-to-speech with Muxlisa & KotibAI. Send a voice note, get a voice reply.

### Streaming Responses
Native Telegram draft updates (Bot API 9.5) — your users see the agent thinking in real-time.

### Plugin System
```python
from qanot.plugins.base import Plugin, tool

class QanotPlugin(Plugin):
    name = "my_plugin"

    @tool("Describe what this tool does")
    async def my_tool(self, params: dict) -> str:
        return '{"result": "done"}'
```
Drop it in the plugins folder. Auto-discovered, hot-loadable, zero config.

### Self-Healing
Autonomous heartbeat every 4 hours: checks pending tasks, validates workspace integrity, consolidates memory, fixes issues silently. You sleep, your agent maintains itself.

### Web Search
Brave API integration for real-time web search. Your agent can look up current information, verify facts, and research topics.

### Extended Thinking
Enable Anthropic's thinking mode for complex reasoning tasks:

```json
{
  "thinking_level": "medium",
  "thinking_budget": 10000
}
```

---

## Architecture

```
User Message → Telegram Adapter (aiogram 3.x)
    → Reaction (acknowledge)
    → WAL scan (write-ahead to SESSION-STATE.md)
    → Image download + downscale (if photo)
    → Voice transcribe (if voice/video note)
    → Link preview extraction (if URLs)
    → Agent loop (max 25 iterations)
        → Build system prompt (SOUL + IDENTITY + SKILL + TOOLS + AGENTS + SESSION-STATE + USER)
        → LLM call (Anthropic / OpenAI / Gemini / Groq)
        → If tool_use: execute → loop
        → If delegation: delegate_to_agent / converse_with_agent → mirror to monitor group
        → If end_turn: log + daily note → respond
    → Reaction (success/error)
    → TTS voice reply (if voice mode enabled)

Heartbeat (every 4h) → Isolated Agent
    → Check workspace: pending tasks, integrity, memory
    → Fix issues silently → Report to monitoring group

Agent Bots → Per-agent Telegram bots (independent polling)
    → Process messages through dedicated Agent instance
    → Can delegate to other agents
    → Group filtering: respond only when @mentioned
```

---

## Quick Start

### Install

```bash
pip install qanot
```

### Initialize

```bash
qanot init mybot
```

The interactive wizard walks you through:
- Telegram bot token (validates via API)
- AI provider selection + API key validation
- Voice provider setup
- User access control

### Run

```bash
qanot start mybot
```

### Docker

```bash
docker build -t qanot .
docker run -v /path/to/data:/data qanot
```

Mount `/data` with your `config.json`, and the framework creates `workspace/`, `sessions/`, `cron/`, and `plugins/` directories automatically.

---

## Configuration

All config lives in a single `config.json`. See [`config.example.json`](config.example.json) for all options.

<details>
<summary><strong>Core options</strong></summary>

| Key | Default | Description |
|-----|---------|-------------|
| `bot_token` | — | Telegram bot token |
| `provider` | `anthropic` | Primary LLM provider |
| `model` | `claude-sonnet-4-6` | Model identifier |
| `providers` | `[]` | Multi-provider failover chain |
| `allowed_users` | `[]` | Telegram user IDs (empty = public) |
| `response_mode` | `stream` | `stream` / `partial` / `blocked` |
| `max_context_tokens` | `200000` | Context window limit |
| `telegram_mode` | `polling` | `polling` / `webhook` |

</details>

<details>
<summary><strong>Agent options</strong></summary>

| Key | Default | Description |
|-----|---------|-------------|
| `agents` | `[]` | Agent definitions for multi-agent |
| `monitor_group_id` | `0` | Telegram group for agent monitoring |

</details>

<details>
<summary><strong>Feature flags</strong></summary>

| Key | Default | Description |
|-----|---------|-------------|
| `rag_enabled` | `true` | Enable RAG semantic search |
| `voice_mode` | `inbound` | `off` / `inbound` / `always` |
| `heartbeat_enabled` | `true` | Enable self-healing |
| `routing_enabled` | `false` | Enable model routing |
| `thinking_level` | `off` | `off` / `low` / `medium` / `high` |
| `backup_enabled` | `true` | Enable workspace backups |
| `brave_api_key` | — | Brave API key for web search |
| `image_api_key` | — | Gemini API key for image generation |

</details>

---

## Built-in Tools

| Tool | Description |
|------|-------------|
| `memory_read` | Read long-term memory |
| `memory_write` | Write to long-term memory |
| `daily_note` | Read/write daily notes |
| `memory_search` | Search across all memory |
| `session_state` | Read/write session state |
| `workspace_list` | List workspace files |
| `web_search` | Search the web (Brave API) |
| `web_fetch` | Fetch and extract URL content |
| `generate_image` | Generate images (Gemini) |
| `rag_search` | Semantic memory search |
| `cron_create` / `cron_list` / `cron_update` / `cron_delete` | Cron job management |
| `delegate_to_agent` / `converse_with_agent` / `spawn_sub_agent` | Agent delegation |
| `create_agent` / `update_agent` / `delete_agent` / `list_agents` | Agent management |
| `view_agent_activity` | Agent activity monitoring |
| `doctor` | System diagnostics |

---

## Compared to Alternatives

| Feature | Qanot | OpenClaw | LangChain |
|---------|-------|----------|-----------|
| Telegram-native | Yes | Yes | No |
| 3-command setup | Yes | Yes | No |
| Multi-agent delegation | Yes | Yes | No |
| Multi-provider failover | Yes | Yes | Yes |
| Built-in RAG | Yes | No | Yes |
| 3-tier memory | Yes | No | No |
| Model routing | Yes | No | No |
| Self-healing | Yes | No | No |
| Image generation | Yes | No | No |
| Voice I/O | Yes | No | No |
| Streaming (native draft) | Yes | No | No |
| Lightweight (pure Python) | Yes | No (Node.js) | No |
| Uzbek voice support | Yes | No | No |

---

## Project Structure

```
qanot/
├── agent.py              # Core agent loop (tool_use cycle, max 25 iterations)
├── agent_bot.py           # Per-agent Telegram bots
├── main.py               # Entry point, wires everything together
├── config.py             # JSON config loader
├── context.py            # Token tracking, working buffer, compaction
├── memory.py             # WAL protocol, daily notes, memory search
├── compaction.py         # Conversation summarization and pruning
├── session.py            # JSONL append-only session logging
├── prompt.py             # System prompt builder
├── telegram.py           # aiogram 3.x adapter (stream/partial/blocked)
├── scheduler.py          # APScheduler cron
├── routing.py            # Model routing (complexity detection)
├── cli.py                # CLI: qanot init/start/version
├── voice.py              # Muxlisa & KotibAI STT/TTS
├── links.py              # URL extraction and link previews
├── backup.py             # Workspace backup with rotation
├── providers/
│   ├── base.py           # LLMProvider ABC, StreamEvent, ProviderResponse
│   ├── anthropic.py      # Claude with streaming + prompt caching
│   ├── openai.py         # GPT with streaming + function calling
│   ├── gemini.py         # Gemini with streaming
│   ├── groq.py           # Groq with streaming
│   ├── errors.py         # Error classification and retry logic
│   └── failover.py       # Multi-provider failover orchestrator
├── plugins/
│   ├── base.py           # Plugin ABC, @tool decorator
│   └── loader.py         # Dynamic plugin discovery
├── rag/
│   ├── engine.py         # RAG query engine (hybrid search)
│   ├── store.py          # sqlite-vec vector store + FTS5
│   ├── embedder.py       # Embedding providers (Gemini, OpenAI)
│   └── chunker.py        # Document chunking
└── tools/
    ├── builtin.py        # Core tools (memory, workspace, web)
    ├── cron.py           # Cron management tools
    ├── delegate.py       # Agent-to-agent delegation
    ├── subagent.py       # Sub-agent spawning
    ├── agent_manager.py  # Runtime agent CRUD
    ├── image.py          # Image generation
    ├── rag.py            # RAG search tools
    ├── web.py            # Web search (Brave API)
    ├── doctor.py         # System diagnostics
    └── workspace.py      # Workspace init + templates
```

---

## Development

```bash
git clone https://github.com/sirli-ai/qanotai.git
cd qanotai
pip install -e .
python -m pytest tests/ -v    # 666 tests
```

---

## License

MIT — use it, fork it, build with it.

---

<p align="center">
  <strong>Built in Tashkent, Uzbekistan</strong><br>
  <sub>Qanot means "wing" — giving your agents the wings to fly.</sub>
</p>
