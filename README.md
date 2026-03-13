<p align="center">
  <h1 align="center">🪶 Qanot AI</h1>
  <p align="center">
    <strong>The AI agent that flies on its own.</strong><br>
    <em>Two commands to fly.</em>
  </p>
  <p align="center">
    <a href="https://pypi.org/project/qanot/"><img src="https://img.shields.io/pypi/v/qanot?color=blue&label=PyPI" alt="PyPI"></a>
    <a href="https://pypi.org/project/qanot/"><img src="https://img.shields.io/pypi/pyversions/qanot" alt="Python"></a>
    <a href="https://github.com/sirli-ai/qanotai/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
    <a href="https://github.com/sirli-ai/qanotai/stargazers"><img src="https://img.shields.io/github/stars/sirli-ai/qanotai?style=social" alt="Stars"></a>
  </p>
</p>

> **Qanot** (Uzbek for "wing") is a lightweight Python framework for building AI-powered Telegram bots with agent loops, multi-agent delegation, persistent memory, RAG, and multi-provider failover — all out of the box.

---

## Why Qanot?

Most agent frameworks give you building blocks and say "good luck." Qanot gives you a **flying agent** in 3 commands:

```bash
pip install qanot
qanot init
```

That's it. The wizard sets up your bot and starts it automatically. Live on Telegram with tool-use, memory, multi-agent delegation, and streaming responses.

---

## What You Get

### 🤖 Agent Loop
Up to 25 tool-use iterations per turn. Your agent thinks, acts, observes, and repeats — autonomously.

### 🤝 Multi-Agent System
Agents that talk to each other. Delegate tasks, hold multi-turn conversations between agents, spawn background workers — with real-time monitoring in a Telegram group.

```
User → Main Agent
         ├── delegate_to_agent("researcher", "find market data")
         ├── converse_with_agent("advisor", "review this plan", rounds=3)
         └── spawn_sub_agent("writer", "draft the report")
```

### 🔀 Multi-Provider Failover
Claude, GPT, Gemini, Groq. If one goes down, Qanot switches automatically with smart cooldowns. Zero downtime.

### 🧠 3-Tier Memory
| Tier | What it does |
|------|-------------|
| **WAL** | Captures corrections, preferences, decisions in real-time |
| **Daily Notes** | Summarizes each day's context automatically |
| **Long-term** | Distilled knowledge that persists forever |

### 🔍 Built-in RAG
Hybrid search (70% semantic + 30% keyword) with sqlite-vec. Your agent remembers documents, not just conversations.

### 🎨 Image Generation
Gemini-powered image creation with workspace storage. Your agent can generate images on demand.

### 🧭 Model Routing
Automatically routes simple queries to cheaper models, complex tasks to powerful ones. Saves 40-60% on API costs.

### 🧪 Extended Thinking
Anthropic thinking mode with configurable budget for complex reasoning tasks.

### 🎙️ Voice I/O
Speech-to-text and text-to-speech with Muxlisa & KotibAI. Send a voice note, get a voice reply.

### ⚡ Streaming Responses
Native Telegram draft updates (Bot API 9.5) — your users see the agent thinking in real-time.

### 🌐 Web Search
Brave API integration for real-time information. Your agent can research topics and verify facts.

### 🔌 Plugin System
```python
from qanot.plugins.base import Plugin, tool

class QanotPlugin(Plugin):
    name = "my_plugin"

    @tool("Describe what this tool does")
    async def my_tool(self, params: dict) -> str:
        return '{"result": "done"}'
```
Drop it in the plugins folder. Auto-discovered, hot-loadable, zero config.

### 🩺 Self-Healing
Autonomous heartbeat every 4 hours: checks pending tasks, validates workspace integrity, consolidates memory, fixes issues silently. You sleep, your agent maintains itself.

### 👥 Group Chat Support
Mention-gated responses in groups, per-group conversation isolation, multi-bot coordination.

### ⏰ Cron Scheduler
Schedule tasks with natural cron syntax. Runs in isolated agent mode — no interference with user conversations.

### 💾 Backup System
Automatic workspace snapshots with configurable rotation. Never lose your agent's memory.

### 📡 Agent Monitoring
Real-time monitoring group where each agent posts as its own bot. See your agents collaborate like a real team chat.

---

## Multi-Agent Setup

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

| Tool | What it does |
|------|-------------|
| `delegate_to_agent` | Send a one-shot task to another agent |
| `converse_with_agent` | Multi-turn conversation between agents |
| `spawn_sub_agent` | Fire-and-forget background task |
| `view_agent_activity` | Real-time activity log |
| `create_agent` / `update_agent` / `delete_agent` | Manage agents at runtime |

---

## Architecture

```
User → Telegram → Agent Loop (25 iterations max)
                      ├── LLM Provider (Claude / GPT / Gemini / Groq)
                      ├── Tool Registry (built-in + plugins)
                      ├── Memory System (WAL → daily notes → long-term)
                      ├── RAG Engine (vector + BM25 hybrid search)
                      ├── Agent Delegation (delegate / converse / spawn)
                      └── Context Tracker (auto-compaction at 70%)

Agent Bots → Per-agent Telegram bots (independent polling)
    → Dedicated Agent instance per bot
    → Cross-agent delegation + group filtering

Heartbeat (every 4h) → Self-healing checks → Silent fixes → Report
```

---

## Quick Start

### Install

```bash
pip install qanot
```

### Initialize

```bash
qanot init
```

The wizard walks you through setup and starts your bot automatically:
- Telegram bot token (validates via API)
- AI provider selection + API key validation
- Voice provider setup
- User access control

### Docker

```bash
docker build -t qanot .
docker run -v /path/to/data:/data qanot
```

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
| `rag_enabled` | `true` | Enable RAG semantic search |
| `voice_mode` | `inbound` | `off` / `inbound` / `always` |
| `heartbeat_enabled` | `true` | Enable self-healing |
| `heartbeat_interval` | `0 */4 * * *` | Heartbeat cron schedule |

</details>

<details>
<summary><strong>Multi-agent options</strong></summary>

| Key | Default | Description |
|-----|---------|-------------|
| `agents` | `[]` | Agent definitions |
| `monitor_group_id` | `0` | Telegram group for monitoring |

</details>

<details>
<summary><strong>Feature flags</strong></summary>

| Key | Default | Description |
|-----|---------|-------------|
| `routing_enabled` | `false` | Enable model routing |
| `routing_model` | — | Cheap model for simple queries |
| `thinking_level` | `off` | `off` / `low` / `medium` / `high` |
| `backup_enabled` | `true` | Enable workspace backups |
| `brave_api_key` | — | Brave API key for web search |
| `image_api_key` | — | Gemini key for image generation |

</details>

---

## Built-in Tools

| Tool | Description |
|------|-------------|
| `memory_read` / `memory_write` | Long-term memory |
| `daily_note` | Daily notes |
| `memory_search` | Search all memory |
| `session_state` | Session state management |
| `workspace_list` | List workspace files |
| `web_search` / `web_fetch` | Web search & fetch |
| `generate_image` | Image generation |
| `rag_search` | Semantic search |
| `cron_create` / `cron_list` / `cron_update` / `cron_delete` | Cron jobs |
| `delegate_to_agent` / `converse_with_agent` / `spawn_sub_agent` | Delegation |
| `create_agent` / `update_agent` / `delete_agent` / `list_agents` | Agent management |
| `doctor` | System diagnostics |

---

## Compared to Alternatives

| Feature | Qanot | OpenClaw | LangChain |
|---------|-------|----------|-----------|
| Telegram-native | ✅ | ✅ | ❌ |
| 3-command setup | ✅ | ✅ | ❌ |
| Multi-agent delegation | ✅ | ✅ | ❌ |
| Multi-provider failover | ✅ | ✅ | ✅ |
| Built-in RAG | ✅ | ❌ | ✅ |
| 3-tier memory | ✅ | ❌ | ❌ |
| Model routing | ✅ | ❌ | ❌ |
| Image generation | ✅ | ❌ | ❌ |
| Self-healing | ✅ | ❌ | ❌ |
| Voice I/O | ✅ | ❌ | ❌ |
| Streaming (native draft) | ✅ | ❌ | ❌ |
| Plugin system | ✅ | ✅ | ✅ |
| Lightweight (pure Python) | ✅ | ❌ | ❌ |
| Uzbek voice support | ✅ | ❌ | ❌ |

---

## Contributing

Contributions are welcome! Please read the existing code patterns before submitting PRs.

```bash
git clone https://github.com/sirli-ai/qanotai.git
cd qanotai
pip install -e .
python -m pytest tests/ -v
```

---

## License

MIT — use it, fork it, build with it.

---

<p align="center">
  <strong>Built in Tashkent, Uzbekistan 🇺🇿</strong><br>
  <sub>Qanot means "wing" — giving your agents the wings to fly.</sub>
</p>
