<p align="center">
  <h1 align="center">Qanot AI</h1>
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
  <p align="center">
    <a href="https://qanot.github.io/docs/">Docs</a> · <a href="https://qanot.github.io/docs/uz/">O'zbekcha</a> · <a href="https://qanot.github.io">Website</a> · <a href="https://t.me/qanot_bot">Telegram</a>
  </p>
</p>

> **Qanot** (Uzbek for "wing") — lightweight Python framework for AI-powered Telegram agents with memory, tools, RAG, multi-agent delegation, and multi-provider failover. All out of the box.

---

## Quick Start

```bash
pip install qanot
qanot init        # Interactive setup — picks provider, model, starts bot
```

That's it. The wizard configures everything and auto-starts your bot.

---

## What Makes Qanot Different

**Agent Loop** — Up to 25 tool-use iterations per turn with circuit breaker, result-aware loop detection, and smart error recovery.

**3-Tier Model Routing** — Routes messages by complexity. "salom" -> Haiku ($0.003). "REST API yozib ber" -> Opus ($0.029). Saves 50-60% on costs.

**1M Context Window** — Auto-detects 1,000,000 tokens for Claude Opus/Sonnet 4.6. Server-side compaction for infinite conversations.

**22 Telegram Commands** — Full settings management from chat: /model, /think, /voice, /lang, /mode, /routing, /mcp, /plugins, /usage, /context, /export, and more. All with inline keyboard buttons.

**Multi-Agent** — Agents that talk to each other. Delegate tasks, hold conversations between agents, spawn background workers.

**5 Providers** — Claude, GPT, Gemini, Groq, Ollama. Automatic failover with smart cooldowns.

**Anthropic Enhancements** — Server-side code execution (free sandbox), trained memory tool, `thinking.display: "omitted"` for faster streaming. All auto-injected for Claude.

**3-Tier Memory** — WAL captures real-time corrections. Daily notes log conversations. Long-term memory in MEMORY.md. Anthropic memory tool (/memories). Agent evolves its own SOUL.md over time. RAG indexes everything.

**RAG** — Hybrid search (vector + FTS5) with FastEmbed CPU embedder. No GPU needed.

**Voice** — 4 providers (Muxlisa, KotibAI, Aisha, Whisper). STT + TTS. Uzbek native.

**Streaming** — Native Telegram `sendMessageDraft` (Bot API 9.5). Real-time, not edit-message hack.

**MCP Client** — Connect to 1000+ community MCP servers (filesystem, Postgres, GitHub, etc.). `pip install qanot[mcp]`

**Browser Control** — Browse, click, fill forms, screenshot via Playwright. `pip install qanot[browser]`

**Skills System** — Agent creates reusable skills (SKILL.md + scripts). Hot-reload without restart.

**Plugins** — amoCRM, Bitrix24, 1C Enterprise, AbsMarket, iBox POS, and more. Build your own with `@tool` decorator.

**Security** — 3-tier exec security (cautious default), per-user rate limiting, SSRF protection, safe file writes, SecretRef.

---

## Architecture

```
User -> Telegram -> Agent Loop (25 iterations max)
                      |-- Model Router (Haiku / Sonnet / Opus)
                      |-- LLM Provider (Claude / GPT / Gemini / Groq / Ollama)
                      |-- Tool Registry (40+ built-in + plugin tools)
                      |-- Memory (WAL -> daily notes -> MEMORY.md -> /memories)
                      |-- RAG Engine (FastEmbed + FTS5 hybrid)
                      |-- Voice Pipeline (4 providers)
                      |-- Multi-Agent (delegate / converse / spawn)
                      |-- MCP Client (external tool servers)
                      |-- Browser (Playwright headless)
                      |-- Skills (hot-reloadable scripts)
                      |-- WebChat (WebSocket adapter)
                      |-- Webhook (external event handler)
                      +-- Security (rate limit + exec approval + file jail)
```

---

## Telegram Commands

All commands have inline keyboard buttons for easy settings management:

| Category | Commands |
|----------|----------|
| **Settings** | /model, /think, /voice, /voiceprovider, /lang, /mode, /routing, /group, /exec, /code |
| **Info** | /status, /usage, /context, /config, /mcp, /plugins, /id |
| **Actions** | /reset, /compact, /export, /stop |
| **Help** | /help |

---

## CLI

```bash
qanot init                  # Setup wizard
qanot start / stop / restart
qanot status / logs
qanot update                # Self-update from PyPI
qanot doctor --fix          # Health check + auto-repair
qanot config show / set
qanot plugin install <name> # Install from registry
qanot plugin new <name>     # Scaffold a plugin
```

---

## Plugins

Ready-made integrations at [QanotHub](https://qanot.github.io/qanot-plugins/):

| Plugin | What it does |
|--------|-------------|
| **amoCRM** | Leads, contacts, companies, tasks, chats, tags, pipelines |
| **Bitrix24** | Deals, leads, contacts, invoices, quotes, products, tasks |
| **1C Enterprise** | Contractors, products, sales, purchases, cash, balances |
| **AbsMarket** | POS sales, purchases, inventory, customers, suppliers |
| **iBox POS** | Products, stock, orders, payments, analytics, ABC analysis |

Build your own:

```python
from qanot.plugins.base import Plugin, tool

class QanotPlugin(Plugin):
    name = "my_plugin"

    @tool("my_tool", "What this tool does")
    async def my_tool(self, params: dict) -> str:
        return '{"result": "done"}'
```

---

## Docs

| | English | O'zbekcha |
|---|---|---|
| Getting Started | [docs](https://qanot.github.io/docs/getting-started/) | [docs](https://qanot.github.io/docs/uz/getting-started/) |
| Full Guide | [docs](https://qanot.github.io/docs/GUIDE/) | [docs](https://qanot.github.io/docs/uz/GUIDE/) |
| Configuration | [docs](https://qanot.github.io/docs/configuration/) | [docs](https://qanot.github.io/docs/uz/configuration/) |
| Tools | [docs](https://qanot.github.io/docs/tools/) | [docs](https://qanot.github.io/docs/uz/tools/) |
| Plugins | [docs](https://qanot.github.io/docs/plugins/) | [docs](https://qanot.github.io/docs/uz/plugins/) |
| Architecture | [docs](https://qanot.github.io/docs/architecture/) | [docs](https://qanot.github.io/docs/uz/architecture/) |
| API Reference | [docs](https://qanot.github.io/docs/api-reference/) | [docs](https://qanot.github.io/docs/uz/api-reference/) |

---

## Contributing

```bash
git clone https://github.com/QANOT/qanot.git
cd qanot
pip install -e .
python -m pytest tests/ -v   # 1007 tests
```

---

## License

MIT — use it, fork it, build with it.

---

<p align="center">
  <strong>Built in Tashkent, Uzbekistan</strong><br>
  <sub>Qanot means "wing" — giving your agents the wings to fly.</sub>
</p>
