<p align="center">
  <h1 align="center">🪶 Qanot AI</h1>
  <p align="center">
    <strong>The AI agent that flies on its own.</strong><br>
    <em>Two commands to fly.</em>
  </p>
  <p align="center">
    <a href="https://pypi.org/project/qanot/"><img src="https://img.shields.io/pypi/v/qanot?color=blue&label=PyPI" alt="PyPI"></a>
    <a href="https://pypi.org/project/qanot/"><img src="https://img.shields.io/badge/python-3.11+-blue" alt="Python 3.11+"></a>
    <a href="https://github.com/QANOT/qanot/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
    <a href="https://github.com/QANOT/qanot/stargazers"><img src="https://img.shields.io/github/stars/QANOT/qanot?style=social" alt="Stars"></a>
  </p>
</p>

> **Qanot** (Uzbek for "wing") is a lightweight Python framework for building AI-powered Telegram bots with agent loops, multi-agent delegation, persistent memory, RAG, and multi-provider failover — all out of the box.

---

## Why Qanot?

Most agent frameworks give you building blocks and say "good luck." Qanot gives you a **flying agent** in one command:

```bash
pip install qanot && qanot init
```

---

## What You Get

### 🤖 Agent Loop
Up to 25 tool-use iterations per turn with circuit breaker: result-aware loop detection, no-progress detection, alternating pattern detection, and deterministic error handling.

### 🧭 3-Tier Model Routing
Automatically routes messages to the right model based on complexity:
```
"salom"                     → Haiku  ($0.003/turn)
"bugun ob-havo qanday?"     → Sonnet ($0.006/turn)
"menga REST API yozib ber"  → Opus   ($0.029/turn)
```
Context-aware: "ha" after Opus tool calling stays on Opus. Saves 50-60% on API costs.

### 🤝 Multi-Agent System
Agents that talk to each other. Delegate tasks, hold multi-turn conversations between agents, spawn background workers — with real-time monitoring in a Telegram group.

### 🔀 Multi-Provider Failover
Claude, GPT, Gemini, Groq, Ollama. If one goes down, Qanot switches automatically with smart cooldowns and thinking level downgrade.

### 🧠 3-Tier Memory + Self-Learning
| Tier | What it does |
|------|-------------|
| **WAL** | Captures corrections, preferences, decisions in real-time |
| **Daily Notes** | Logs each conversation automatically |
| **Long-term** | Agent curates MEMORY.md — distilled knowledge that persists |

The agent **learns from mistakes**: errors are logged to daily notes, and the agent evolves its own SOUL.md, IDENTITY.md, and MEMORY.md over time.

### 🔍 Built-in RAG
Hybrid search (vector + FTS5 keyword) with FastEmbed CPU embedder. Works with Ollama without VRAM conflict.

### 🎙️ 4 Voice Providers
| Provider | STT | TTS | Languages |
|----------|-----|-----|-----------|
| **Muxlisa** | ✅ | ✅ | Uzbek (native OGG) |
| **KotibAI** | ✅ | ✅ | uz/ru/en (6 voices) |
| **Aisha AI** | ✅ | ✅ | uz/en/ru (mood control) |
| **Whisper** | ✅ | — | 50+ languages |

### ⚡ Streaming Responses
Native Telegram `sendMessageDraft` (Bot API 9.5) — real-time streaming, not edit-message hack.

### 🔐 Security
- **3-tier exec security**: `open` / `cautious` / `strict` with inline approval buttons
- **Per-user rate limiting**: sliding window + lockout (OpenClaw-inspired)
- **Safe file write**: blocks system dirs, symlinks, path traversal
- **SecretRef**: load API keys from env vars or files, not plain config
- **SSRF protection**: on web fetch and voice download
- **Command blocklist**: 30+ dangerous patterns

### 🏠 Ollama / Local LLM
Zero-config local model support:
```bash
qanot init    # Select "Ollama" → auto-detects models
```
- Ollama native API with `think=false` (30x faster for Qwen)
- FastEmbed CPU embedder (no VRAM conflict)
- Works offline, 100% private

### 🌐 Web & Links
- **Web search** (Brave API)
- **Web fetch** with SSRF protection
- **Link understanding**: auto-fetches URLs in messages and injects previews

### 🔌 Plugin System
```python
from qanot.plugins.base import Plugin, tool

class QanotPlugin(Plugin):
    name = "my_plugin"

    @tool("Describe what this tool does")
    async def my_tool(self, params: dict) -> str:
        return '{"result": "done"}'
```

### 📁 File Sharing
Agent can send files to users via Telegram — workspace files, generated reports, anything.

### 📊 Web Dashboard
Live web UI for monitoring your agent: conversation logs, cost breakdown, tool usage stats, and memory inspection. Runs on a local port alongside the bot.

### 🧪 Synthetic User Testing
`agent_eval.py` — automated evaluation harness that simulates multi-turn user conversations, measures tool accuracy, response quality, and cost per scenario. Run it before deploying to catch regressions.

### 🩺 Self-Healing
Autonomous heartbeat, workspace backup rotation, daily briefing, memory consolidation.

### ⏰ Cron Scheduler
Schedule tasks with cron syntax. Runs in isolated agent mode.

---

## CLI Commands

```bash
qanot init               # Interactive setup wizard
qanot start              # Start bot (auto-installs OS service)
qanot stop               # Stop bot
qanot restart            # Restart bot
qanot status             # Check if running
qanot logs               # Tail bot logs
qanot update             # Self-update from PyPI + restart
qanot config show        # Show current configuration
qanot config set <k> <v> # Change a config value
qanot config add-provider # Add a backup AI provider
qanot doctor             # Health checks (--fix to auto-repair)
qanot backup             # Export workspace to .tar.gz
qanot plugin new <name>  # Scaffold a new plugin
```

---

## Architecture

```
User → Telegram → Agent Loop (25 iterations max)
                      ├── Model Router (Haiku / Sonnet / Opus)
                      ├── LLM Provider (Claude / GPT / Gemini / Groq / Ollama)
                      ├── Tool Registry (35+ built-in tools + plugins)
                      ├── Memory System (WAL → daily notes → long-term)
                      ├── RAG Engine (FastEmbed + FTS5 hybrid)
                      ├── Voice Pipeline (4 providers: STT + TTS)
                      ├── Agent Delegation (delegate / converse / spawn)
                      ├── Security (rate limit + exec approval + file jail)
                      ├── Web Dashboard (live monitoring UI)
                      └── Context Tracker (auto-compaction at 60%)
```

---

## Built-in Tools

| Tool | Description |
|------|-------------|
| `read_file` / `write_file` / `list_files` | File operations |
| `send_file` | Send files to user via Telegram |
| `run_command` | Shell execution (3-tier security) |
| `memory_search` | Search memory and daily notes |
| `session_status` / `cost_status` | Session and cost info |
| `web_search` / `web_fetch` | Web search & fetch |
| `generate_image` / `edit_image` | Image generation |
| `rag_search` / `rag_index` | RAG semantic search |
| `cron_create` / `cron_list` / `cron_update` / `cron_delete` | Cron jobs |
| `delegate_to_agent` / `converse_with_agent` / `spawn_sub_agent` | Multi-agent |
| `create_agent` / `update_agent` / `delete_agent` | Agent management |
| `doctor` | System diagnostics |

---

## Compared to Alternatives

| Feature | Qanot | OpenClaw | LangChain/LangGraph |
|---------|-------|----------|---------------------|
| Telegram-native | ✅ | ✅ | ❌ (needs integration) |
| Multi-channel (Discord, Slack, etc.) | ❌ Telegram only | ✅ 22 channels | ❌ (needs integration) |
| 2-command setup | ✅ | ✅ | ❌ (code required) |
| Multi-agent delegation | ✅ | ✅ | ✅ LangGraph |
| Multi-provider failover | ✅ | ✅ | ✅ |
| Built-in RAG | ✅ hybrid | ✅ hybrid | ✅ (via chains) |
| Memory system | ✅ 3-tier (WAL+daily+long-term) | ✅ 2-tier | ✅ LangGraph checkpoints |
| Model routing | ✅ 3-tier auto | ✅ alias-based | ❌ |
| Self-learning (workspace evolve) | ✅ | ✅ | ❌ |
| Voice I/O | ✅ 4 providers | ✅ ElevenLabs | ❌ |
| Streaming | ✅ sendMessageDraft | ✅ partial edit | ✅ |
| Browser automation | ❌ | ✅ CDP/Chrome | ❌ |
| Exec security | ✅ 3-tier + inline buttons | ✅ sandbox + approvals | ❌ |
| Web dashboard | ✅ | ✅ (Control UI) | ✅ LangSmith |
| Per-user cost tracking | ✅ | ❌ | ✅ LangSmith |
| Ollama / local LLM | ✅ zero-config | ❌ | ✅ (via config) |
| Lightweight | ✅ 143MB RAM | ❌ 1.9GB RAM | ⚠️ varies |
| Observability | ✅ dashboard + eval | ⚠️ basic logs | ✅ LangSmith |
| Uzbek voice (Muxlisa, Aisha) | ✅ | ❌ | ❌ |
| Community & ecosystem | 🌱 new | ✅ 313K stars, 5700 skills | ✅ 34M downloads/mo |

---

## Contributing

Contributions are welcome! Please read the existing code patterns before submitting PRs.

```bash
git clone https://github.com/QANOT/qanot.git
cd qanotai
pip install -e .
python -m pytest tests/ -v   # 757 tests
```

---

## License

MIT — use it, fork it, build with it.

---

<p align="center">
  <strong>Built in Tashkent, Uzbekistan 🇺🇿</strong><br>
  <sub>Qanot means "wing" — giving your agents the wings to fly.</sub>
</p>
