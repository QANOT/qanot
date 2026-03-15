# Qanot AI v2.0 User Guide

Lightweight Python agent framework for Telegram bots. Built for the Uzbekistan market.

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [Configuration](#2-configuration)
3. [Providers](#3-providers)
4. [Voice](#4-voice)
5. [Model Routing](#5-model-routing)
6. [Security](#6-security)
7. [Memory](#7-memory)
8. [RAG](#8-rag)
9. [Multi-Agent](#9-multi-agent)
10. [Dashboard](#10-dashboard)
11. [CLI Reference](#11-cli-reference)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Quick Start

Three steps to a running bot.

### Step 1: Install

```bash
pip install qanot
```

### Step 2: Initialize

```bash
qanot init
```

The interactive wizard walks you through:

- Telegram bot token (from [@BotFather](https://t.me/BotFather))
- AI provider selection (Anthropic, OpenAI, Gemini, Groq, or Ollama)
- API key validation
- Voice support (optional)
- Web search (optional)

It creates a `config.json`, a `workspace/` directory with a default `SOUL.md`, and the `sessions/`, `cron/`, and `plugins/` directories.

### Step 3: Start

```bash
qanot start
```

The bot installs itself as an OS service (launchd on macOS, systemd on Linux) and starts in the background. The first person to message the bot becomes the owner.

To run in the foreground (useful for Docker or debugging):

```bash
qanot start -f
```

---

## 2. Configuration

All configuration lives in `config.json`. The `qanot init` wizard generates it, but you can edit it manually.

### Full Config Reference

```jsonc
{
  // ── Core ──
  "bot_token": "123456:ABC...",        // Telegram bot token from @BotFather
  "provider": "anthropic",             // Primary provider: anthropic|openai|gemini|groq
  "model": "claude-sonnet-4-6",        // Primary model
  "api_key": "sk-ant-...",             // Primary API key

  // ── Multi-provider (optional) ──
  "providers": [                       // Additional providers for failover
    {
      "name": "anthropic-main",
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "api_key": "sk-ant-..."
    },
    {
      "name": "gemini-backup",
      "provider": "gemini",
      "model": "gemini-2.5-flash",
      "api_key": "AIza..."
    }
  ],

  // ── Identity ──
  "owner_name": "Sirojiddin",          // Your name (bot uses it in conversation)
  "bot_name": "Qanot",                 // Bot persona name
  "timezone": "Asia/Tashkent",         // IANA timezone

  // ── Paths ──
  "workspace_dir": "/data/workspace",  // SOUL.md, MEMORY.md, daily notes
  "sessions_dir": "/data/sessions",    // JSONL session logs
  "cron_dir": "/data/cron",            // Scheduled jobs (jobs.json)
  "plugins_dir": "/data/plugins",      // Plugin directories

  // ── Context ──
  "max_context_tokens": 200000,        // Max context window (depends on model)
  "compaction_mode": "safeguard",      // "safeguard" = auto-compact at 60%
  "max_memory_injection_chars": 4000,  // Max chars injected from RAG/compaction
  "history_limit": 50,                 // Max turns restored from session on restart

  // ── Telegram ──
  "response_mode": "stream",           // "stream"|"partial"|"blocked"
  "stream_flush_interval": 0.8,        // Seconds between draft updates (stream mode)
  "telegram_mode": "polling",          // "polling"|"webhook"
  "webhook_url": "",                   // Required if telegram_mode is "webhook"
  "webhook_port": 8443,                // Local port for webhook server
  "max_concurrent": 4,                 // Max concurrent message processing

  // ── Access Control ──
  "allowed_users": [],                 // Telegram user IDs (empty = public)

  // ── Voice ──
  "voice_provider": "muxlisa",         // "muxlisa"|"kotib"|"aisha"|"whisper"
  "voice_api_key": "",                 // Default voice API key (fallback)
  "voice_api_keys": {                  // Per-provider keys
    "muxlisa": "",
    "kotib": ""
  },
  "voice_mode": "inbound",             // "off"|"inbound"|"always"
  "voice_name": "",                    // TTS voice name
  "voice_language": "",                // Force STT language (uz/ru/en), empty = auto

  // ── RAG ──
  "rag_enabled": true,                 // Enable memory search via RAG
  "rag_mode": "auto",                  // "auto"|"agentic"|"always"

  // ── Web Search ──
  "brave_api_key": "",                 // Brave Search API key (free: 2000 queries/month)

  // ── UX ──
  "reactions_enabled": false,          // Send emoji reactions on messages
  "reply_mode": "coalesced",           // "off"|"coalesced"|"always"
  "group_mode": "mention",              // "off"|"mention"|"all"

  // ── Self-Healing ──
  "heartbeat_enabled": true,           // Enable periodic self-check
  "heartbeat_interval": "0 */4 * * *", // Every 4 hours

  // ── Daily Briefing ──
  "briefing_enabled": true,            // Morning summary
  "briefing_schedule": "0 8 * * *",    // 8:00 AM daily

  // ── Extended Thinking (Claude) ──
  "thinking_level": "off",             // "off"|"low"|"medium"|"high"
  "thinking_budget": 10000,            // Max thinking tokens

  // ── Execution Security ──
  "exec_security": "open",             // "open"|"cautious"|"strict"
  "exec_allowlist": [],                // Commands allowed in strict mode

  // ── Model Routing ──
  "routing_enabled": false,            // Route simple messages to cheaper model
  "routing_model": "claude-haiku-4-5-20251001",  // Cheap model for greetings
  "routing_mid_model": "claude-sonnet-4-6",       // Mid-tier model
  "routing_threshold": 0.3,            // Complexity threshold (0.0-1.0)

  // ── Image Generation ──
  "image_api_key": "",                 // Gemini key for image generation
  "image_model": "gemini-3-pro-image-preview",

  // ── Dashboard ──
  "dashboard_enabled": true,           // Web UI at :8765
  "dashboard_port": 8765,

  // ── Backup ──
  "backup_enabled": true,              // Auto-backup on startup

  // ── Multi-Agent ──
  "agents": [],                        // Agent definitions (see Multi-Agent section)
  "monitor_group_id": 0,               // Telegram group ID for agent monitoring

  // ── Plugins ──
  "plugins": []                        // Plugin configurations
}
```

### Changing Config After Init

```bash
# Show current config
qanot config show

# Change a value
qanot config set model claude-opus-4-6
qanot config set response_mode partial
qanot config set exec_security cautious

# Add a backup provider (interactive)
qanot config add-provider

# Restart for changes to take effect
qanot restart
```

---

## 3. Providers

Qanot supports five AI providers. You can configure multiple providers for failover.

### Anthropic (Claude)

Supports both standard API keys and OAuth tokens (from Claude Code).

**Standard API key:**

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-api03-..."
}
```

**OAuth token (Claude Code):**

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-oat01-..."
}
```

OAuth tokens automatically enable Claude Code identity headers, which gives access to Opus and Sonnet models. The provider detects `sk-ant-oat` prefix and configures headers accordingly.

**Available models:**

| Model | Use Case |
|---|---|
| `claude-sonnet-4-6` | Fast, recommended for most tasks |
| `claude-opus-4-6` | Most capable, best for complex reasoning |
| `claude-haiku-4-5-20251001` | Cheapest, good for simple queries |

**Extended thinking:**

```json
{
  "thinking_level": "medium",
  "thinking_budget": 10000
}
```

Levels: `off`, `low`, `medium`, `high`. Only works with Anthropic.

### OpenAI (GPT)

```json
{
  "provider": "openai",
  "model": "gpt-4.1",
  "api_key": "sk-proj-..."
}
```

**Available models:**

| Model | Use Case |
|---|---|
| `gpt-4.1` | Latest, recommended |
| `gpt-4.1-mini` | Fast and cheap |
| `gpt-4o` | Multimodal |
| `gpt-4o-mini` | Cheapest |

### Google Gemini

```json
{
  "provider": "gemini",
  "model": "gemini-2.5-flash",
  "api_key": "AIza..."
}
```

Gemini uses the OpenAI-compatible endpoint internally (`https://generativelanguage.googleapis.com/v1beta/openai/`). It also provides free embedding for RAG.

**Available models:**

| Model | Use Case |
|---|---|
| `gemini-2.5-flash` | Fast, recommended |
| `gemini-2.5-pro` | Most capable |
| `gemini-2.0-flash` | Cheapest |

### Groq

```json
{
  "provider": "groq",
  "model": "llama-3.3-70b-versatile",
  "api_key": "gsk_..."
}
```

Groq uses the OpenAI-compatible API (`https://api.groq.com/openai/v1`).

**Available models:**

| Model | Use Case |
|---|---|
| `llama-3.3-70b-versatile` | Recommended |
| `llama-3.1-8b-instant` | Fastest |
| `qwen/qwen3-32b` | Qwen 3 |

### Ollama (Local)

Free and private. Runs locally, no API key needed.

```json
{
  "providers": [
    {
      "name": "ollama-main",
      "provider": "openai",
      "model": "qwen3.5:35b",
      "api_key": "ollama",
      "base_url": "http://localhost:11434/v1"
    }
  ]
}
```

Note that Ollama uses `"provider": "openai"` because it speaks the OpenAI-compatible API. The framework detects Ollama by the `11434` port in `base_url` and automatically:

- Disables thinking mode for Qwen models (30x faster)
- Uses the native Ollama API with `think=false`
- Uses FastEmbed (CPU) for RAG embeddings to avoid VRAM conflict

**Install and run Ollama:**

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3.5:35b
```

### Multi-Provider Failover

Configure multiple providers. Qanot tries the primary first, then falls back:

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-...",
  "providers": [
    {
      "name": "anthropic-main",
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "api_key": "sk-ant-..."
    },
    {
      "name": "gemini-backup",
      "provider": "gemini",
      "model": "gemini-2.5-flash",
      "api_key": "AIza..."
    }
  ]
}
```

---

## 4. Voice

Qanot supports four voice providers for speech-to-text (STT) and text-to-speech (TTS).

### Voice Modes

| Mode | Behavior |
|---|---|
| `off` | Voice messages ignored |
| `inbound` | Transcribes incoming voice; replies with voice when appropriate |
| `always` | Always replies with voice |

### Muxlisa.uz (Default)

Native Uzbek provider. Accepts OGG directly (no ffmpeg conversion for STT).

```json
{
  "voice_provider": "muxlisa",
  "voice_api_keys": {
    "muxlisa": "your-muxlisa-api-key"
  },
  "voice_mode": "inbound",
  "voice_name": "maftuna"
}
```

**Voices:** `maftuna`, `asomiddin`

Get an API key at [muxlisa.uz](https://muxlisa.uz).

### KotibAI

6 voices, multi-language support, auto language detection.

```json
{
  "voice_provider": "kotib",
  "voice_api_keys": {
    "kotib": "your-jwt-token"
  },
  "voice_mode": "inbound",
  "voice_name": "aziza"
}
```

**Voices:** `aziza`, `sherzod`, and 4 more

Get a JWT token at [developer.kotib.ai](https://developer.kotib.ai).

### Aisha AI

STT + TTS with mood detection. Supports Uzbek, English, and Russian.

```json
{
  "voice_provider": "aisha",
  "voice_api_keys": {
    "aisha": "your-aisha-api-key"
  },
  "voice_mode": "inbound"
}
```

**Voices:** `Gulnoza`, `Jaxongir`

Get an API key at [aisha.group](https://aisha.group).

### OpenAI Whisper

STT only (no TTS). High accuracy, 50+ languages.

```json
{
  "voice_provider": "whisper",
  "voice_api_keys": {
    "whisper": "sk-proj-..."
  },
  "voice_mode": "inbound"
}
```

Get an API key at [platform.openai.com](https://platform.openai.com).

### Using Multiple Voice Providers

You can configure multiple providers and set per-provider keys:

```json
{
  "voice_provider": "muxlisa",
  "voice_api_keys": {
    "muxlisa": "key-for-muxlisa",
    "kotib": "jwt-for-kotib",
    "whisper": "sk-proj-for-whisper"
  },
  "voice_mode": "inbound"
}
```

### Force STT Language

By default, language is auto-detected. To force a specific language:

```json
{
  "voice_language": "uz"
}
```

Options: `uz`, `ru`, `en`, or empty for auto-detection.

---

## 5. Model Routing

3-tier model routing saves money by sending simple messages to cheaper models.

### How It Works

1. **Tier 1 (Cheap):** Greetings, thanks, simple questions go to `routing_model` (e.g., Haiku)
2. **Tier 2 (Mid):** General conversation goes to `routing_mid_model` (e.g., Sonnet)
3. **Tier 3 (Full):** Complex tasks, tool use, reasoning stay on the primary `model`

The agent scores each incoming message for complexity (0.0 to 1.0). Messages below `routing_threshold` go to the cheap tier.

### Configuration

```json
{
  "routing_enabled": true,
  "routing_model": "claude-haiku-4-5-20251001",
  "routing_mid_model": "claude-sonnet-4-6",
  "routing_threshold": 0.3,
  "model": "claude-opus-4-6"
}
```

### Example Cost Savings

| Message | Complexity | Routed To |
|---|---|---|
| "Salom!" | 0.05 | Haiku ($0.80/MTok) |
| "Bugun ob-havo qanday?" | 0.15 | Haiku |
| "Loyiha strukturasini tushuntir" | 0.45 | Sonnet ($3/MTok) |
| "Bu kodni refaktor qil va testlar yoz" | 0.85 | Opus ($15/MTok) |

With routing enabled, typical bots see a 40-60% cost reduction.

---

## 6. Security

### Execution Security Modes

The `run_command` tool has three security levels for shell command execution.

#### open (Default)

Only dangerous commands are blocked (rm -rf /, fork bombs, disk fill attacks, etc.). Everything else runs freely.

```json
{
  "exec_security": "open"
}
```

#### cautious

Dangerous commands are blocked. Risky commands (pip install, curl, sudo, docker, git push, etc.) require user approval via inline button or text confirmation.

```json
{
  "exec_security": "cautious"
}
```

The bot will ask: "Bu buyruqni bajarishga ruxsat berasizmi: `pip install requests`?" and wait for approval.

#### strict

Only allowlisted commands are permitted. Everything else is rejected.

```json
{
  "exec_security": "strict",
  "exec_allowlist": [
    "git status",
    "git log",
    "git diff",
    "python",
    "ls",
    "cat"
  ]
}
```

Allowlist entries are prefix-matched: `"git"` allows `git status`, `git log`, etc.

### Always-Blocked Commands

These are blocked in all modes (including `open`):

- `rm -rf /` and variants (recursive delete of root/home)
- `mkfs`, `dd`, `shred` (disk destruction)
- `shutdown`, `reboot`, `poweroff`
- `chmod 777 /`, `chown root`
- Network attack tools (nmap, sqlmap, hydra, metasploit)
- `curl | sh`, `wget | sh` (remote code execution)
- Fork bombs, disk fill attacks

### Rate Limiting

Use `max_concurrent` to limit parallel message processing:

```json
{
  "max_concurrent": 4
}
```

Use `allowed_users` to restrict who can use the bot:

```json
{
  "allowed_users": [123456789, 987654321]
}
```

Empty array means the bot is public. The first user to message becomes the owner automatically.

### SecretRef

Never store API keys as plain text in `config.json`. Use SecretRef to load them from environment variables or files.

**From environment variable:**

```json
{
  "api_key": {"env": "ANTHROPIC_API_KEY"},
  "bot_token": {"env": "TELEGRAM_BOT_TOKEN"},
  "brave_api_key": {"env": "BRAVE_API_KEY"}
}
```

**From file:**

```json
{
  "api_key": {"file": "/run/secrets/anthropic_key"},
  "bot_token": {"file": "/run/secrets/bot_token"}
}
```

File-based secrets have security checks:
- Symlinks are rejected (prevents escape attacks)
- World-readable files produce a warning (recommend `chmod 600`)
- Maximum 64 KB file size

SecretRef works for: `api_key`, `bot_token`, `brave_api_key`, `voice_api_key`, `image_api_key`, all provider `api_key` fields, and `voice_api_keys` values.

### File Jail

The `write_file` tool blocks writes to system directories:

- `/etc`, `/usr`, `/bin`, `/sbin`, `/lib`, `/boot`, `/proc`, `/sys`, `/dev`
- `/System`, `/Library` (macOS)
- `C:\Windows`, `C:\Program Files` (Windows)

Symlink writes are also blocked to prevent directory traversal.

---

## 7. Memory

Qanot has a three-tier memory system that lets the bot learn and remember across conversations.

### WAL Protocol (Write-Ahead Log)

Every user message is scanned BEFORE the LLM responds. The WAL detects:

| Category | Trigger Examples (English) | Trigger Examples (Uzbek) |
|---|---|---|
| Corrections | "actually", "no, I meant" | "aslida", "to'g'ri emas" |
| Proper nouns | "my name is Ahmad" | "mening ismim Ahmad" |
| Preferences | "I like Python", "I prefer dark mode" | "men Python yoqtiraman" |
| Decisions | "let's use React" | "keling React ishlataylik" |
| Specific values | URLs, dates, large numbers | Same |
| Remember | "remember this", "don't forget" | "eslab qol", "unutma" |

Detected entries are written to `SESSION-STATE.md` (active working memory). Durable facts (names, preferences, explicit "remember" requests) are also saved to `MEMORY.md`.

### File Structure

```
workspace/
  MEMORY.md           # Long-term curated facts
  SESSION-STATE.md    # Active session working memory (WAL entries)
  memory/
    2026-03-15.md     # Daily notes (conversation summaries)
    2026-03-14.md
    ...
```

### MEMORY.md

Long-term storage for facts the bot should always remember. Written automatically by WAL for durable categories, and by the agent during compaction (memory flush).

Example:

```markdown
# MEMORY.md - Long-Term Memory

## Auto-captured

- **proper_noun**: [user:123] mening ismim Ahmad
- **preference**: [user:123] I prefer Python over JavaScript
- **remember**: [user:123] remember this: project deadline is March 30
```

### Daily Notes

When context is compacted, the agent saves conversation summaries to `memory/YYYY-MM-DD.md`. These are searchable via the `memory_search` tool and indexed by RAG.

### Context Compaction

At 60% context usage, the working buffer activates. When context overflows, Qanot:

1. Runs a memory flush (agent saves important facts to files)
2. Summarizes the conversation
3. Replaces old messages with the summary
4. Continues with fresh context

The `compaction_mode: "safeguard"` setting ensures this happens automatically.

### How the Agent Learns

```
User message → WAL scan → Detect facts → Write SESSION-STATE.md / MEMORY.md
                                        ↓
LLM response → Tool calls → Daily notes → memory/2026-03-15.md
                                        ↓
Context overflow → Compact → Summary → Fresh context with injected memories
                                        ↓
Next message → RAG search → Inject relevant memories → LLM sees full context
```

---

## 8. RAG

RAG (Retrieval-Augmented Generation) enhances the bot's memory search with semantic understanding.

### How It Works

Qanot uses hybrid search combining:

1. **FTS5** (SQLite full-text search) -- keyword matching, always available
2. **Vector embeddings** -- semantic similarity, requires an embedding provider

Results are fused using reciprocal rank fusion with temporal decay (recent memories rank higher).

### Embedding Provider Chain

Qanot auto-detects the best available embedder from your existing config. No extra API keys required.

| Priority | Provider | Dimensions | Cost | When Used |
|---|---|---|---|---|
| 0 | FastEmbed (CPU) | 768 | Free | Ollama setups (no VRAM conflict) |
| 1 | Gemini | 3072 | Free tier | When Gemini API key available |
| 2 | OpenAI | 1536 | $0.02/MTok | When OpenAI API key available |
| - | FTS-only | - | Free | Fallback when no embedder available |

### Configuration

RAG is enabled by default. No additional configuration needed if you have a Gemini or OpenAI key.

```json
{
  "rag_enabled": true,
  "rag_mode": "auto"
}
```

**RAG modes:**

| Mode | Behavior |
|---|---|
| `auto` | RAG searches automatically when relevant |
| `agentic` | Agent decides when to search via `memory_search` tool |
| `always` | Always injects RAG results into context |

### Gemini Embedding Setup

If you have a Gemini API key (even as a non-primary provider), embeddings work automatically:

```json
{
  "providers": [
    {
      "name": "gemini-embed",
      "provider": "gemini",
      "model": "gemini-2.5-flash",
      "api_key": "AIza..."
    }
  ]
}
```

The framework uses `gemini-embedding-001` (3072 dimensions) with an embedding cache to avoid re-embedding unchanged content.

### Ollama Embedding Setup

For Ollama users, install FastEmbed for CPU-based embedding (avoids VRAM conflict):

```bash
pip install fastembed
```

If FastEmbed is not installed, Ollama falls back to its own embedding via the `nomic-embed-text` model.

### What Gets Indexed

- `MEMORY.md` -- long-term facts
- `SESSION-STATE.md` -- active session state
- `memory/*.md` -- daily notes (last 30 files)

Files are re-indexed when their content changes (hash-based deduplication).

---

## 9. Multi-Agent

Qanot supports multi-agent collaboration with three interaction patterns.

### Agent Definition

Define agents in `config.json`:

```json
{
  "agents": [
    {
      "id": "deep-researcher",
      "name": "Chuqur Tadqiqotchi",
      "prompt": "You are a deep research agent. Use web_search and web_fetch extensively. Investigate topics thoroughly with multiple sources. Always cite your sources.",
      "model": "claude-opus-4-6",
      "bot_token": "",
      "tools_allow": ["web_search", "web_fetch", "memory_search", "read_file"],
      "tools_deny": [],
      "delegate_allow": [],
      "max_iterations": 15,
      "timeout": 180
    },
    {
      "id": "fast-coder",
      "name": "Tezkor Dasturchi",
      "prompt": "You are a fast coding agent. Write clean, working code quickly. Follow existing project conventions.",
      "model": "claude-haiku-4-5-20251001",
      "bot_token": "",
      "tools_deny": ["web_search", "web_fetch"],
      "delegate_allow": ["deep-researcher"],
      "timeout": 60
    }
  ]
}
```

### Agent Fields

| Field | Description |
|---|---|
| `id` | Unique identifier (e.g., `"researcher"`, `"coder"`) |
| `name` | Human-readable name (e.g., `"Tadqiqotchi"`) |
| `prompt` | System prompt / personality |
| `model` | Model override (empty = use main model) |
| `provider` | Provider override (empty = use main provider) |
| `api_key` | API key override (empty = use main key) |
| `bot_token` | Separate Telegram bot token (empty = internal agent only) |
| `tools_allow` | Tool whitelist (empty = all tools allowed) |
| `tools_deny` | Tool blacklist |
| `delegate_allow` | Which other agents this one can delegate to (empty = all) |
| `max_iterations` | Max tool-use loops (default: 15) |
| `timeout` | Seconds before timeout (default: 120) |

### Interaction Patterns

#### 1. delegate_to_agent

One-shot task handoff. The main agent sends a task, the sub-agent completes it and returns the result.

```
User: "Bu mavzu haqida chuqur tadqiqot qil"
Bot: [delegates to deep-researcher agent]
     [deep-researcher runs web searches, reads sources]
     [returns findings to main agent]
Bot: "Tadqiqot natijalari: ..."
```

#### 2. converse_with_agent

Multi-turn ping-pong conversation between agents (up to 5 turns). Useful when agents need to iterate on a solution.

#### 3. spawn_sub_agent

Creates a background agent that runs independently. Results are posted to a shared project board.

### Shared Project Board

Agents can post results to a shared board that other agents can read. This enables asynchronous collaboration without direct message passing.

### Agent Monitoring

Mirror all agent conversations to a Telegram group for monitoring:

```json
{
  "monitor_group_id": -1001234567890
}
```

### Access Control

Use `delegate_allow` to control which agents can talk to each other:

```json
{
  "agents": [
    {
      "id": "coder",
      "delegate_allow": ["researcher"]
    },
    {
      "id": "researcher",
      "delegate_allow": []
    }
  ]
}
```

Here, `coder` can delegate to `researcher`, but `researcher` cannot delegate to anyone. Maximum delegation depth is 2 to prevent infinite loops.

---

## 10. Dashboard

Qanot includes a web dashboard for real-time monitoring.

### Enable

```json
{
  "dashboard_enabled": true,
  "dashboard_port": 8765
}
```

The dashboard starts automatically with the bot. Access it at `http://localhost:8765`.

### API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Web UI (HTML dashboard) |
| `GET /api/status` | Bot status: uptime, context %, token count, active conversations |
| `GET /api/config` | Current configuration (no secrets) |
| `GET /api/costs` | Per-user cost tracking |
| `GET /api/memory` | Memory file listing |
| `GET /api/memory/{filename}` | Read a specific memory file |
| `GET /api/tools` | Registered tools list |
| `GET /api/routing` | Model routing statistics |

### Example: Check Bot Status

```bash
curl http://localhost:8765/api/status
```

```json
{
  "bot_name": "Qanot",
  "model": "claude-sonnet-4-6",
  "provider": "anthropic",
  "uptime": "2h 15m 30s",
  "context_percent": 23.5,
  "total_tokens": 45200,
  "turn_count": 12,
  "api_calls": 28,
  "buffer_active": false,
  "active_conversations": 3
}
```

---

## 11. CLI Reference

### Commands

| Command | Description |
|---|---|
| `qanot init [dir]` | Interactive setup wizard. Creates config.json and workspace. |
| `qanot start [path]` | Start the bot via OS service (launchd/systemd). |
| `qanot start -f` | Start in foreground (for Docker, systemd, debugging). |
| `qanot stop [path]` | Stop the bot. |
| `qanot restart [path]` | Restart the bot (stop + start). |
| `qanot status [path]` | Check if the bot is running. |
| `qanot logs [path]` | Tail bot logs (`-n50` for line count). |
| `qanot doctor [path]` | Run health checks on the installation. |
| `qanot doctor --fix` | Auto-repair detected issues. |
| `qanot backup [path]` | Export workspace/sessions/cron to `.tar.gz`. |
| `qanot config show` | Show current configuration. |
| `qanot config set <key> <value>` | Set a config value. |
| `qanot config add-provider` | Add a backup AI provider (interactive). |
| `qanot config remove-provider` | Remove an AI provider. |
| `qanot plugin new <name>` | Scaffold a new plugin directory. |
| `qanot plugin list` | List installed plugins. |
| `qanot update` | Update to latest version from PyPI + restart. |
| `qanot version` | Show installed version. |
| `qanot help` | Show help. |

### Environment Variables

| Variable | Description |
|---|---|
| `QANOT_CONFIG` | Path to config.json (overrides default lookup). |

### Config Path Resolution

The CLI searches for `config.json` in this order:

1. Positional argument (file or directory)
2. `QANOT_CONFIG` environment variable
3. `./config.json` (current directory)
4. `/data/config.json` (Docker default)

---

## 12. Troubleshooting

### Bot Not Starting

**Symptom:** `qanot start` shows no output or errors.

```bash
# Check status
qanot status

# Check logs
qanot logs

# Run in foreground to see errors
qanot start -f
```

**Common causes:**

- Invalid `bot_token` -- verify with @BotFather
- Invalid `api_key` -- check provider dashboard
- Port conflict on `webhook_port` or `dashboard_port`

### "Config file not found"

```bash
# Check if config exists
ls config.json

# Point to the right location
export QANOT_CONFIG=/path/to/config.json
qanot start
```

Or run `qanot init` to create a new config.

### API Key Invalid

```bash
# Run health checks
qanot doctor

# The doctor validates bot token and API keys
```

If using SecretRef:

```bash
# Check that the env var is set
echo $ANTHROPIC_API_KEY

# Check that the file exists and is readable
cat /run/secrets/anthropic_key
```

### Context Overflow / "Kontekst to'ldi"

The bot auto-compacts at 60% context usage. If you see frequent compaction:

```json
{
  "max_context_tokens": 200000,
  "history_limit": 30,
  "max_memory_injection_chars": 2000
}
```

- Increase `max_context_tokens` if your model supports it
- Decrease `history_limit` to restore fewer turns on restart
- Decrease `max_memory_injection_chars` to inject less RAG context

### Voice Not Working

```bash
qanot doctor
```

Check the "Voice" section in doctor output. Common issues:

- **Missing API key:** Set `voice_api_keys.muxlisa` (or your provider)
- **ffmpeg not installed:** Required for KotibAI and Whisper (Muxlisa accepts OGG directly)
- **Wrong voice_mode:** Must be `"inbound"` or `"always"`, not `"off"`

Install ffmpeg:

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

### Bot Responds Slowly

1. **Check response mode:** `"stream"` is fastest (uses Telegram Bot API 9.5 `sendMessageDraft`)
2. **Enable routing:** Route simple messages to cheaper/faster models
3. **Check model:** Haiku/Flash models respond faster than Opus/Pro
4. **Check concurrent limit:** Increase `max_concurrent` if many users

```json
{
  "response_mode": "stream",
  "routing_enabled": true,
  "max_concurrent": 8
}
```

### RAG Not Finding Memories

```bash
# Check if RAG is enabled
qanot config show

# Check logs for embedder initialization
qanot logs | grep -i "embedder\|rag"
```

If you see "FTS-only mode", no embedding provider was found. Add a Gemini key for free vector search:

```json
{
  "providers": [
    {
      "name": "gemini-embed",
      "provider": "gemini",
      "model": "gemini-2.5-flash",
      "api_key": "AIza..."
    }
  ]
}
```

### Bot Stuck in Loop

The circuit breaker stops the bot after 3 identical consecutive tool calls (`MAX_SAME_ACTION = 3`). If the bot seems stuck:

- Check if a tool is returning deterministic errors (the agent injects `_hint` for permanent failures)
- Maximum 25 iterations per turn (`MAX_ITERATIONS = 25`)
- For delegation tools, timeout is 5 minutes (`LONG_TOOL_TIMEOUT = 300`)

### Stale Sessions / High Disk Usage

```bash
# Run doctor with auto-fix
qanot doctor --fix

# Or manually create a backup and clean up
qanot backup
```

Doctor auto-archives sessions older than 30 days and warns when session files exceed 100 MB.

### Plugin Not Loading

```bash
# List plugins and their status
qanot plugin list

# Check doctor output
qanot doctor
```

Ensure the plugin is:
1. In the `plugins_dir` directory
2. Has a `plugin.py` file with a `QanotPlugin` class
3. Listed in `config.json` `plugins` array
4. Has `"enabled": true`

### Webhook Mode Not Working

```json
{
  "telegram_mode": "webhook",
  "webhook_url": "https://bot.example.com/webhook",
  "webhook_port": 8443
}
```

Requirements:
- `webhook_url` must be HTTPS with a valid certificate
- Port must be one of: 443, 80, 88, or 8443
- The server must be reachable from Telegram's IP ranges

If unsure, use `"telegram_mode": "polling"` (the default).
