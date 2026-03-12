# Qanot AI — Project Guidelines

Lightweight Python agent framework for Telegram bots (PyPI: `qanot`).
Alternative to OpenClaw, built for Uzbekistan market needs.

## Architecture

```
qanot/
├── agent.py          # Core agent loop (tool_use cycle, max 25 iterations)
├── main.py           # Entry point, wires everything together
├── config.py         # JSON config loader → Config dataclass
├── context.py        # Token tracking, 60% threshold, Working Buffer
├── memory.py         # WAL protocol, daily notes, memory search
├── session.py        # JSONL append-only session logging
├── prompt.py         # System prompt builder (8 sections)
├── telegram.py       # aiogram 3.x adapter (stream/partial/blocked modes)
├── scheduler.py      # APScheduler cron (isolated + systemEvent modes)
├── cli.py            # CLI: qanot init/start/version
├── providers/
│   ├── base.py       # LLMProvider ABC, StreamEvent, ProviderResponse
│   ├── anthropic.py  # Claude with streaming + prompt caching
│   └── openai.py     # GPT with streaming + function calling adapter
├── plugins/
│   ├── base.py       # Plugin ABC, @tool decorator
│   └── loader.py     # Dynamic plugin discovery
└── tools/
    ├── builtin.py    # 6 built-in tools
    ├── cron.py       # 4 cron management tools
    └── workspace.py  # Workspace init + templates
```

## Key Design Decisions

- **Per-user conversation isolation**: `Agent._conversations` dict keyed by user_id
- **3 response modes**: `stream` (sendMessageDraft), `partial` (editMessageText), `blocked` (wait+send)
- **WAL Protocol**: Scan every user message for corrections/preferences/decisions BEFORE responding
- **Working Buffer**: Auto-activates at 60% context usage for compaction recovery
- **Streaming**: Provider yields `StreamEvent` deltas → Agent `run_turn_stream()` → Telegram adapter flushes on interval

## Development

```bash
python3 -m pytest tests/ -v    # Run tests (58 tests)
python3 -m qanot               # Run via __main__
qanot start                    # Run via CLI entry point
```

## Sub-Agents (.claude/agents/)

Use these to delegate work without losing project context:

| Agent | When to Use |
|---|---|
| `qanot-architect` | New features, design decisions, architecture evaluation |
| `qanot-impl` | Writing code, implementing specs, adding tests |
| `qanot-review` | Code review, catch regressions, security/perf audit |
| `qanot-debug` | Trace bugs through agent loop → provider → telegram pipeline |

## Slash Commands (.claude/commands/)

| Command | What It Does |
|---|---|
| `/push` | Auto-analyze changes, write commit message, commit & push |
| `/fix <description>` | Trace bug → root cause → minimal fix → test |
| `/add <feature>` | Design → implement → test following project conventions |
| `/improve [area]` | Audit → prioritize → targeted improvements only |
| `/review [area]` | Review uncommitted changes or specific code for issues |
| `/test [area]` | Run tests, analyze failures, identify coverage gaps |
| `/status` | Full project health: git, tests, architecture, open issues |

## Rules

- Never add Co-Authored-By to commits
- Always verify external APIs via web search before claiming features exist/don't exist
- Config changes must update: Config dataclass, load_config(), config.example.json
- New tools must be registered in main.py and documented in templates/workspace/TOOLS.md
- Per-user isolation: never use shared mutable state across users in agent loop
- Streaming: pause draft updates during tool execution to avoid race conditions (learned from OpenClaw bugs)
