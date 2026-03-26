# Architecture

This page describes the internal structure of Qanot AI: the agent loop, data flow, and how components connect.

## System Overview

```
                         Telegram
                            |
                     TelegramAdapter
                      (aiogram 3.x)
                            |
                    +-------+-------+
                    |               |
                  Agent          CronScheduler
               (per-user)       (APScheduler)
                    |               |
              +-----+-----+   spawn_isolated_agent()
              |     |     |
           Provider |  ToolRegistry
           (LLM)   |     |
              |    Context  +---> Built-in Tools
              |   Tracker   +---> Cron Tools
              |             +---> RAG Tools
              |             +---> Plugin Tools
              |             +---> MCP Tools
              |             +---> Browser Tools
              |             +---> Skill Tools
              |
        +-----+-----+
        |     |     |
   Anthropic OpenAI Gemini Groq
        |     |     |     |
        +--FailoverProvider--+
```

## Startup Sequence

`qanot/main.py` orchestrates initialization in this order:

1. **Load config** -- `load_config()` reads `config.json`
2. **Init workspace** -- `init_workspace()` copies templates on first run
3. **Create provider** -- Single provider or `FailoverProvider` for multi-provider
4. **Create context tracker** -- Token tracking for the session
5. **Create tool registry** -- Empty registry
6. **Init RAG engine** (if enabled) -- Create embedder, vector store, RAG engine; index workspace memory files
7. **Register built-in tools** -- `read_file`, `write_file`, `list_files`, `run_command`, `web_search`, `memory_search`, `session_status`
8. **Create session writer** -- JSONL log writer
9. **Create cron scheduler** -- APScheduler with tool registry reference
10. **Register cron tools** -- `cron_create`, `cron_list`, `cron_update`, `cron_delete`
11. **Load plugins** -- Discover, import, setup, register plugin tools
12. **Create agent** -- Wire provider, tools, session, context
13. **Register RAG tools** -- `rag_index`, `rag_search`, `rag_list`, `rag_forget` (needs agent reference)
14. **Register memory hooks** -- Wire RAG indexer to memory write events
15. **Register MCP tools** (if enabled) -- Connect to MCP servers, register discovered tools
16. **Register browser tools** (if enabled) -- Register Playwright-based browser tools
17. **Register skill tools** -- Register skill management tools
18. **Register memory tool** (if enabled) -- Register Anthropic memory tool operations
19. **Run lifecycle hooks** -- Execute on_startup hooks
20. **Start scheduler** -- Load jobs, start APScheduler
21. **Start webhook/webchat** (if enabled) -- Start webhook endpoint and/or webchat WebSocket server
22. **Start Telegram** -- Start polling or webhook server

## Agent Loop

The core agent loop runs up to 25 iterations per user message:

```
User message
    |
    v
WAL Protocol scan (corrections, preferences, decisions)
    |
    v
Compaction recovery check (inject working buffer if needed)
    |
    v
Add message to conversation history
    |
    +---> [Loop start: iteration 1..25]
    |         |
    |     Proactive compaction check (if > 60%, compact)
    |         |
    |     Repair messages (fix orphaned tool_results)
    |         |
    |     Build system prompt (from workspace files)
    |         |
    |     Call LLM provider (with retry for transient errors)
    |         |
    |     Track token usage
    |         |
    |     +--- stop_reason == "tool_use" ---+
    |     |                                  |
    |     |   Check for tool call loops      |
    |     |   (3x same call, A-B-A-B)        |
    |     |        |                         |
    |     |   Execute tools (30s timeout)    |
    |     |        |                         |
    |     |   Add results to history         |
    |     |        |                         |
    |     |   [Continue loop]                |
    |     |                                  |
    |     +--- stop_reason == "end_turn" ----+
    |     |                                  |
    |     |   Final text response            |
    |     |   Log to session                 |
    |     |   Append to working buffer       |
    |     |   Write daily note               |
    |     |   [Return response]              |
    |     |                                  |
    |     +--- other / max iterations -------+
    |                                        |
    v                                        v
  Response text                           Error message
```

### Streaming Variant

`run_turn_stream()` follows the same loop but yields `StreamEvent` objects:

- `text_delta` -- text fragment from the LLM
- `tool_use` -- tool execution happening (no text to show)
- `done` -- final response with full `ProviderResponse`

The streaming variant has a fallback: if streaming fails with a transient error, it retries once with non-streaming `chat()`.

## Per-User Isolation

Each Telegram user gets an isolated conversation state:

```python
Agent._conversations: dict[str | None, list[dict]]
#   key: user_id string (or None for cron jobs)
#   value: message history list

Agent._locks: dict[str | None, asyncio.Lock]
#   per-user lock for write safety

Agent._last_active: dict[str | None, float]
#   monotonic timestamp for idle eviction
```

- Messages from different users never mix
- Per-user locks prevent concurrent processing of messages from the same user
- Conversations idle for more than 1 hour (3600s) are automatically evicted
- Cron jobs use `None` as user_id for their own isolated conversations

## System Prompt Assembly

`build_system_prompt()` assembles the prompt from workspace files:

```
1. SOUL.md          -- Core personality and instructions
2. IDENTITY.md      -- Agent name, style, emoji preferences
3. SKILL.md         -- Proactive agent behaviors
4. TOOLS.md         -- Tool documentation
5. *_TOOLS.md       -- Plugin tool documentation
6. AGENTS.md        -- Operating rules
7. SESSION-STATE.md -- WAL entries (active session context)
8. USER.md          -- Human context
9. BOOTSTRAP.md     -- First-run ritual (if file exists)
+ Tool call style rules (hardcoded)
+ Session info (date, time, context %, tokens)
```

**Minimal mode** (used for cron isolated agents): Only SOUL.md + TOOLS.md + session info.

**Character budget:**
- Per file: 20,000 chars max (70% head / 20% tail truncation)
- Total prompt: 150,000 chars max

Variables `{date}`, `{bot_name}`, `{owner_name}`, `{timezone}` are replaced in the final prompt.

## Streaming Pipeline

```
LLM Provider
    |
    | yields StreamEvent(type="text_delta", text="...")
    v
Agent.run_turn_stream()
    |
    | yields StreamEvent to caller
    v
TelegramAdapter._respond_stream()
    |
    | accumulates text
    | sends draft at flush_interval
    v
Bot.sendMessageDraft(chat_id, draft_id, text)
    |
    | final
    v
Bot.sendMessage(chat_id, formatted_html)
```

Key points:
- Draft updates are paused during tool execution to avoid race conditions
- The Telegram adapter tracks the last sent draft text to avoid redundant updates
- Each streaming session gets a unique `draft_id`
- The final message is sent with HTML formatting (Markdown is converted)

## Error Handling and Failover Flow

```
Agent calls provider.chat()
    |
    +--- Success --> return response
    |
    +--- Exception caught
    |        |
    |    classify_error(e) --> error_type
    |        |
    |    +--- PERMANENT (auth, billing)
    |    |       --> raise immediately
    |    |
    |    +--- TRANSIENT (rate_limit, overloaded, timeout)
    |    |       --> retry with exponential backoff (2s, 4s, max 30s)
    |    |       --> up to 2 retries
    |    |
    |    +--- UNKNOWN
    |            --> raise immediately
    |
    [If all retries fail]
        |
        +--- rate_limit --> "Limitga yetdik..."
        +--- auth       --> "API kalitda xatolik..."
        +--- billing    --> "API hisob muammosi..."
        +--- other      --> "Xatolik yuz berdi..."
```

With `FailoverProvider`, the flow extends:

```
FailoverProvider.chat()
    |
    Try active provider
    |     |
    |   Success --> mark_success(), return
    |     |
    |   Failure --> classify_error(), mark_failed()
    |                   |
    |               cooldown = 120s * failure_count (max 600s)
    |                   |
    Try next available provider
    |     ...
    |
    All providers exhausted --> raise last error
```

## Context Management Flow

```
Turn N: input_tokens = 45,000 / 200,000 max (22.5%)
    --> Normal operation

Turn N+5: input_tokens = 100,000 (50%)
    --> Working buffer ACTIVATES
    --> Exchanges logged to working-buffer.md

Turn N+10: estimated next = 128,000 (64% > 60% threshold)
    --> Proactive compaction triggers
    --> Messages: [first 2] + [summary marker] + [last 4]
    --> Token estimate adjusted to ~35%

Turn N+20: compaction detected in messages
    --> Recovery context injected from:
        - working-buffer.md
        - SESSION-STATE.md
        - today's daily notes
```

## Session Logging

Every message exchange is logged to JSONL files in the sessions directory:

```
sessions/
├── 2025-01-15.jsonl     # Regular conversations
├── cron-heartbeat-20250115-160000.jsonl  # Cron job sessions
```

Each line is a JSON object:

```json
{
  "type": "message",
  "id": "msg_000001",
  "parentId": "",
  "timestamp": "2025-01-15T10:30:00+00:00",
  "message": {"role": "user", "content": "Hello"},
}
```

Assistant messages include usage stats and model information. File writes use cross-platform locking (`fcntl.LOCK_EX` on Unix, graceful degradation on Windows).

## Data Flow Summary

| Data | Written By | Read By |
|------|-----------|---------|
| `config.json` | User | `load_config()` |
| `SOUL.md`, `TOOLS.md`, etc. | User / Agent / Plugins | `build_system_prompt()` |
| `SESSION-STATE.md` | WAL protocol | System prompt, `memory_search` |
| `memory/*.md` (daily notes) | Agent loop | `memory_search`, RAG indexer |
| `MEMORY.md` | Agent (via tools) | `memory_search`, RAG indexer |
| `memory/working-buffer.md` | Context tracker | Compaction recovery |
| `sessions/*.jsonl` | Session writer | External monitoring tools |
| `cron/jobs.json` | Cron tools / User | Cron scheduler |
| `rag.db` | RAG engine | RAG search |
| `uploads/*` | Telegram adapter | Agent (via `read_file`) |

## Module Reference

Beyond the core modules described above, Qanot includes these additional components:

### Core Modules

| Module | Purpose |
|--------|---------|
| `agent.py` | Core agent loop (25 iterations, circuit breaker, result-aware loops) |
| `agent_bot.py` | Separate agent bot runtime |
| `backup.py` | Startup backup functionality |
| `config.py` | JSON config loader, `Config` dataclass, `SecretRef` |
| `context.py` | Token tracking, 50% buffer, 60% compaction threshold |
| `compaction.py` | Multi-stage LLM summarization (OpenClaw-style) |
| `routing.py` | 3-tier model routing (Haiku/Sonnet/Opus) |
| `voice.py` | Voice provider integration (Muxlisa, KotibAI, Aisha, Whisper) |
| `ratelimit.py` | Per-user sliding window rate limiter |
| `links.py` | Auto URL preview injection |
| `utils.py` | Utility functions (truncation, helpers) |
| `fs_safe.py` | Safe file write (system dir block, symlink check) |
| `secrets.py` | SecretRef resolver (env vars, files) |
| `session.py` | JSONL append-only session logging (cross-platform locking) |
| `prompt.py` | System prompt builder (9 sections + MEMORY.md injection) |
| `telegram.py` | aiogram 3.x adapter (stream/partial/blocked + inline buttons) |
| `dashboard.py` | Web dashboard server at :8765 (aiohttp) |
| `dashboard_html.py` | Dashboard HTML (Bloomberg Terminal aesthetic) |
| `daemon.py` | Cross-platform daemon (systemd/launchd/schtasks) |
| `scheduler.py` | APScheduler cron (isolated + systemEvent modes) |
| `cli.py` | CLI: init/start/stop/restart/status/config/update/doctor |
| `mcp_client.py` | MCP (Model Context Protocol) client for external tool servers |
| `webhook.py` | Webhook endpoint for external events (GitHub, CRM, CI/CD) |
| `webchat.py` | WebChat adapter with WebSocket streaming |
| `hooks.py` | Lifecycle hooks system (on_startup, on_shutdown, on_pre_turn, on_post_turn) |

### Tool Modules (`tools/`)

| Module | Purpose |
|--------|---------|
| `builtin.py` | read/write/list/run_command/send_file/memory/session/cost |
| `cron.py` | 4 cron management tools |
| `web.py` | web_search (Brave) + web_fetch (SSRF protected) |
| `image.py` | generate_image + edit_image (Gemini) |
| `rag.py` | 4 RAG tools (search/index/list/forget) |
| `delegate.py` | Multi-agent delegation (delegate/converse/spawn) |
| `subagent.py` | Sub-agent management |
| `agent_manager.py` | create/update/delete/restart agents |
| `doctor.py` | System diagnostics |
| `workspace.py` | Workspace init + templates |
| `jobs_io.py` | Cron jobs JSON I/O utilities |
| `memory_tool.py` | Anthropic memory tool (view/create/str_replace/insert/delete/rename) |
| `browser.py` | Browser tools via Playwright (browse_url, click, fill_form, screenshot, extract_data) |
| `skill_tools.py` | Skill management tools (create_skill, list_skills, run_skill_script, delete_skill) |
