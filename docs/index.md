# Qanot AI

Lightweight Python agent framework for building AI-powered Telegram bots.

**PyPI:** `qanot` | **Python:** 3.11+ | **License:** MIT

## What is Qanot AI?

Qanot AI is a framework that turns an LLM into a persistent, tool-using Telegram assistant. You provide a config file and a Telegram bot token, and Qanot handles the agent loop, memory, streaming, cron scheduling, and multi-provider failover.

Built for the Uzbekistan market: defaults to `Asia/Tashkent` timezone, Telegram-first design (Telegram is the dominant messaging platform in Uzbekistan), and Uzbek-language error messages.

## Key Features

- **Multi-provider support** -- Anthropic Claude, OpenAI GPT, Google Gemini, Groq. Switch providers in config without code changes.
- **Automatic failover** -- configure multiple providers and Qanot switches between them on errors with cooldown tracking.
- **Live streaming** -- real-time response streaming via Telegram Bot API 9.5 `sendMessageDraft`, with `editMessageText` and blocked fallbacks.
- **RAG (Retrieval-Augmented Generation)** -- built-in document indexing with hybrid search (vector + BM25). Uses sqlite-vec for local vector storage.
- **Memory system** -- WAL protocol scans every message for corrections and preferences before responding. Daily notes, session state, and long-term memory files.
- **Context management** -- token tracking with automatic compaction at 70% usage and working buffer activation at 60%.
- **Cron scheduler** -- APScheduler-based scheduled tasks with isolated agent spawning or system event injection.
- **Plugin system** -- extend with custom tools via a decorator-based plugin API.
- **Per-user isolation** -- separate conversation histories per Telegram user with automatic eviction of idle conversations.

## How It Compares to OpenClaw

| Aspect | Qanot AI | OpenClaw |
|--------|----------|----------|
| Size | Lightweight (~15 files) | Heavy (many modules) |
| Providers | 4 built-in + failover | Typically single provider |
| Streaming | Native `sendMessageDraft` | `editMessageText` only |
| RAG | Built-in hybrid search | External dependency |
| Memory | WAL protocol + daily notes | Basic memory |
| Context | Auto-compaction + working buffer | Manual management |
| Market focus | Uzbekistan (timezone, Telegram) | General |

## Quick Start

```bash
# 1. Install
pip install qanot

# 2. Create a project
qanot init mybot

# 3. Configure (edit bot_token and api_key)
nano mybot/config.json

# 4. Run
qanot start mybot
```

Your bot is now live on Telegram. Send it a message.

## Documentation

- [Getting Started](getting-started.md) -- installation, first bot, configuration walkthrough
- [Configuration Reference](configuration.md) -- every config field explained
- [LLM Providers](providers.md) -- provider setup, failover, custom providers
- [Memory System](memory.md) -- WAL protocol, daily notes, working buffer
- [RAG](rag.md) -- document indexing, hybrid search, memory integration
- [Tools](tools.md) -- built-in tools, cron tools, RAG tools
- [Plugin System](plugins.md) -- creating custom tools and plugins
- [Telegram Integration](telegram.md) -- response modes, streaming, webhooks
- [Cron Scheduler](scheduler.md) -- scheduled tasks, heartbeat, proactive messaging
- [Architecture](architecture.md) -- system design, agent loop, data flow
- [API Reference](api-reference.md) -- class and method documentation

## Requirements

- Python 3.11+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- At least one LLM API key (Anthropic, OpenAI, Gemini, or Groq)
- Optional: `sqlite-vec` for RAG vector search (`pip install qanot[rag]`)
