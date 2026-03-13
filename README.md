<p align="center">
  <h1 align="center">🪶 Qanot AI</h1>
  <p align="center">
    <strong>Build intelligent Telegram agents in minutes, not months.</strong>
  </p>
  <p align="center">
    <a href="https://pypi.org/project/qanot/"><img src="https://img.shields.io/pypi/v/qanot?color=blue&label=PyPI" alt="PyPI"></a>
    <a href="https://pypi.org/project/qanot/"><img src="https://img.shields.io/pypi/pyversions/qanot" alt="Python"></a>
    <a href="https://github.com/QANOT/qanotai/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
    <a href="https://github.com/QANOT/qanotai/stargazers"><img src="https://img.shields.io/github/stars/QANOT/qanotai?style=social" alt="Stars"></a>
  </p>
</p>

> **Qanot** (Uzbek for "wing") is a lightweight Python framework for building AI-powered Telegram bots with agent loops, persistent memory, RAG, and multi-provider failover — all out of the box.

---

## Why Qanot?

Most agent frameworks give you building blocks and say "good luck." Qanot gives you a **flying agent** in 3 commands:

```bash
pip install qanot
qanot init mybot
qanot start mybot
```

That's it. Your bot is live on Telegram with tool-use, memory, and streaming responses.

---

## What You Get

### 🤖 Agent Loop
Up to 25 tool-use iterations per turn. Your agent thinks, acts, observes, and repeats — autonomously.

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

### 🎙️ Voice I/O
Speech-to-text and text-to-speech with Muxlisa & KotibAI. Send a voice note, get a voice reply.

### ⚡ Streaming Responses
Native Telegram draft updates (Bot API 9.5) — your users see the agent thinking in real-time.

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

### ⏰ Cron Scheduler
Schedule tasks with natural cron syntax. Runs in isolated agent mode — no interference with user conversations.

---

## Architecture

```
User → Telegram → Agent Loop (25 iterations max)
                      ├── LLM Provider (Claude / GPT / Gemini / Groq)
                      ├── Tool Registry (built-in + plugins)
                      ├── Memory System (WAL → daily notes → long-term)
                      ├── RAG Engine (vector + BM25 hybrid search)
                      └── Context Tracker (auto-compaction at 70%)

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

---

## Configuration

All config lives in a single `config.json`. See [`config.example.json`](config.example.json) for all options.

<details>
<summary><strong>Key options</strong></summary>

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

---

## Documentation

| Doc | Description |
|-----|-------------|
| [Getting Started](docs/getting-started.md) | Full setup guide |
| [Architecture](docs/architecture.md) | System design deep dive |
| [Configuration](docs/configuration.md) | All config options |
| [Providers](docs/providers.md) | Multi-provider setup |
| [Tools](docs/tools.md) | Built-in tools reference |
| [Plugins](docs/plugins.md) | Plugin development guide |
| [RAG](docs/rag.md) | Retrieval-augmented generation |
| [Memory](docs/memory.md) | Memory system internals |
| [Scheduler](docs/scheduler.md) | Cron scheduling |
| [Telegram](docs/telegram.md) | Telegram adapter config |

---

## Compared to Alternatives

| Feature | Qanot | OpenClaw | LangChain |
|---------|-------|----------|-----------|
| Telegram-native | ✅ | ✅ | ❌ |
| 3-command setup | ✅ | ✅ | ❌ |
| Multi-provider failover | ✅ | ✅ | ✅ |
| Built-in RAG | ✅ | ❌ | ✅ |
| 3-tier memory | ✅ | ❌ | ❌ |
| Self-healing | ✅ | ❌ | ❌ |
| Voice I/O | ✅ | ❌ | ❌ |
| Plugin system | ✅ | ✅ | ✅ |
| Lightweight | ✅ | ✅ | ❌ |
| Uzbek voice support | ✅ | ❌ | ❌ |

---

## Contributing

Contributions are welcome! Please read the existing code patterns before submitting PRs.

```bash
git clone https://github.com/QANOT/qanotai.git
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
