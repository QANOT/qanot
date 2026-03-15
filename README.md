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
  <p align="center">
    <a href="https://plane.topkey.uz/docs/">Docs</a> · <a href="https://plane.topkey.uz/docs/uz/">O'zbekcha</a> · <a href="https://plane.topkey.uz">Website</a> · <a href="https://t.me/qanot_bot">Telegram</a>
  </p>
</p>

> **Qanot** (Uzbek for "wing") — lightweight Python framework for AI-powered Telegram agents with memory, tools, RAG, multi-agent delegation, and multi-provider failover. All out of the box.

---

## Quick Start

```bash
pip install qanot
qanot init        # Interactive setup — picks provider, model, creates config
qanot start       # Bot is live
```

That's it. Your agent is running.

---

## What Makes Qanot Different

**Agent Loop** — Up to 25 tool-use iterations per turn with circuit breaker, result-aware loop detection, and smart error recovery.

**3-Tier Model Routing** — Routes messages by complexity. "salom" → Haiku ($0.003). "REST API yozib ber" → Opus ($0.029). Saves 50-60% on costs.

**Multi-Agent** — Agents that talk to each other. Delegate tasks, hold conversations between agents, spawn background workers.

**5 Providers** — Claude, GPT, Gemini, Groq, Ollama. Automatic failover with smart cooldowns.

**3-Tier Memory** — WAL captures real-time corrections. Daily notes log conversations. Long-term memory persists in MEMORY.md. Agent evolves its own SOUL.md over time.

**RAG** — Hybrid search (vector + FTS5) with FastEmbed CPU embedder. No GPU needed.

**Voice** — 4 providers (Muxlisa, KotibAI, Aisha, Whisper). STT + TTS. Uzbek native.

**Streaming** — Native Telegram `sendMessageDraft` (Bot API 9.5). Real-time, not edit-message hack.

**115+ Plugin Tools** — amoCRM (34), Bitrix24 (30), 1C Enterprise (18), AbsMarket (32). Build your own with `@tool` decorator.

**Security** — 3-tier exec security, per-user rate limiting, SSRF protection, safe file writes, SecretRef.

---

## Architecture

```
User → Telegram → Agent Loop (25 iterations max)
                      ├── Model Router (Haiku / Sonnet / Opus)
                      ├── LLM Provider (Claude / GPT / Gemini / Groq / Ollama)
                      ├── Tool Registry (35+ built-in + 115 plugin tools)
                      ├── Memory (WAL → daily notes → long-term)
                      ├── RAG Engine (FastEmbed + FTS5 hybrid)
                      ├── Voice Pipeline (4 providers)
                      ├── Multi-Agent (delegate / converse / spawn)
                      └── Security (rate limit + exec approval + file jail)
```

---

## CLI

```bash
qanot init                  # Setup wizard
qanot start / stop / restart
qanot status / logs
qanot update                # Self-update from PyPI
qanot doctor --fix          # Health check + auto-repair
qanot config show / set
qanot plugin new <name>     # Scaffold a plugin
```

---

## Plugins

Ready-made integrations:

| Plugin | Tools | What it does |
|--------|-------|-------------|
| **amoCRM** | 34 | Leads, contacts, companies, tasks, chats, tags, pipelines |
| **Bitrix24** | 30 | Deals, leads, contacts, invoices, quotes, products, tasks |
| **1C Enterprise** | 18 | Contractors, products, sales, purchases, cash, balances |
| **AbsMarket** | 32 | POS sales, purchases, inventory, customers, suppliers + SQL |

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
| Getting Started | [docs](https://plane.topkey.uz/docs/getting-started/) | [docs](https://plane.topkey.uz/docs/uz/getting-started/) |
| Full Guide | [docs](https://plane.topkey.uz/docs/GUIDE/) | [docs](https://plane.topkey.uz/docs/uz/GUIDE/) |
| Configuration | [docs](https://plane.topkey.uz/docs/configuration/) | [docs](https://plane.topkey.uz/docs/uz/configuration/) |
| Tools | [docs](https://plane.topkey.uz/docs/tools/) | [docs](https://plane.topkey.uz/docs/uz/tools/) |
| Plugins | [docs](https://plane.topkey.uz/docs/plugins/) | [docs](https://plane.topkey.uz/docs/uz/plugins/) |
| Architecture | [docs](https://plane.topkey.uz/docs/architecture/) | [docs](https://plane.topkey.uz/docs/uz/architecture/) |
| API Reference | [docs](https://plane.topkey.uz/docs/api-reference/) | [docs](https://plane.topkey.uz/docs/uz/api-reference/) |

---

## Contributing

```bash
git clone https://github.com/QANOT/qanot.git
cd qanot
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
