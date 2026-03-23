# Changelog

All notable changes to Qanot AI are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [2.0.4] - 2026-03-23

### Added
- 22 Telegram slash commands with inline keyboard buttons (OpenClaw-style settings management)
  - Settings: /model, /think, /voice, /voiceprovider, /lang, /mode, /routing, /group, /exec, /code
  - Info: /status, /usage, /context, /config, /id, /mcp, /plugins
  - Actions: /reset (with model hint), /compact, /export, /stop
- Anthropic server-side code execution (`code_execution_20250825`) — free with web search
- Anthropic memory tool (`memory_20250818`) with dual-layer architecture
  - All providers get /memories tool (view, create, str_replace, insert, delete, rename)
  - Anthropic gets trained memory behavior (auto-check, structured notes)
  - RAG indexes /memories directory automatically
- /mcp command — view connected MCP servers, tool counts, connection status
- /plugins command — list/enable/disable plugins via inline buttons
- Lifecycle hooks system (on_startup, on_shutdown, on_pre_turn, on_post_turn)
- Webhook endpoint for external events (GitHub, CRM, CI/CD)
- WebChat adapter with WebSocket streaming
- 1M context window auto-detection for Opus 4.6 / Sonnet 4.6
- `thinking.display: "omitted"` for faster time-to-first-token
- Opus 4.6 added to pricing table
- `dashboard_host` config field (0.0.0.0 for Docker, 127.0.0.1 default)

### Fixed
- WebChat auth: token from URL param instead of embedded HTML
- Stream tool_use events now pass tool_call data correctly
- WebChat WSS behind nginx uses correct host
- Webhook/webchat routes registered before dashboard start (frozen router)

## [2.0.3] - 2026-03-22

### Added
- MCP (Model Context Protocol) client — connect to 1000+ external tool servers
- Browser control via Playwright — browse_url, click, fill_form, screenshot, extract_data
- Skill management tools — create_skill, list_skills, run_skill_script, delete_skill
- Agent self-improving: creates reusable SKILL.md + scripts for repetitive tasks
- Skills hot-reload without restart
- Optional deps: `pip install qanot[mcp]`, `pip install qanot[browser]`

## [2.0.2] - 2026-03-22

### Fixed
- Compaction loop when context usage exceeded threshold ratio
- Cross-user file delivery race in `_send_pending_files`
- Dangling `tool_use` block when no-progress detected (missing `tool_result`)
- JSONDecodeError crash when tool returns non-JSON in deterministic error check
- Division-by-zero in BM25 search when average document length is zero
- Dashboard bound to `0.0.0.0` — now defaults to `127.0.0.1`
- Path traversal in RAG indexer via symlinks outside workspace
- Rate limiter accepting `max_requests=0` without error

### Security
- Default `exec_security` changed from `"open"` to `"cautious"`
- Added `validate_read_path` — `read_file`, `list_files`, `send_file` now block system directories
- Expanded `.gitignore` for sessions, databases, env variants

### Added
- Public API exports: `from qanot import Agent, Config, Plugin, tool, LLMProvider`
- `py.typed` marker for mypy/pyright support
- GitHub Actions CI (Python 3.11/3.12/3.13)
- CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md
- LICENSE file (MIT)
- Full PyPI metadata (authors, URLs, classifiers, keywords)
- 15 new document types (20 total) for Uzbek business law

### Changed
- `aiomysql` moved from core to optional `mysql` extra

## [2.0.0] - 2026-03-12

### Added
- Multi-agent delegation system (delegate, converse, spawn)
- Dynamic agent management (create/update/delete at runtime)
- Image generation and editing via Gemini
- 3-tier model routing (Haiku/Sonnet/Opus by complexity)
- Extended thinking support (Claude reasoning mode)
- Agent monitoring and group mirroring
- Web dashboard (Bloomberg Terminal aesthetic) at :8765
- Cross-platform daemon (systemd/launchd/schtasks)
- APScheduler cron with isolated + systemEvent modes
- 115+ plugin tools (amoCRM, Bitrix24, 1C Enterprise, AbsMarket)
- Document generation (DOCX, XLSX, PDF, PPTX — 20 types)
- Native Telegram streaming via `sendMessageDraft` (Bot API 9.5)

### Changed
- Complete architecture rewrite from v1.x
- Agent loop now 25 iterations with circuit breaker
- Memory system: 3-tier WAL + daily notes + long-term
- Provider system: 5 providers with automatic failover

## [1.1.0] - 2025-12

### Added
- RAG engine with hybrid search (vector + FTS5)
- Multi-stage compaction (OpenClaw-style)
- Web search (Brave API) and web fetch (SSRF protected)
- Voice messages (4 providers: Muxlisa, KotibAI, Aisha, Whisper)
- Group chat support with mention/reply modes
- Daily morning briefing cron job
- Doctor diagnostics and backup rotation
- Multi-provider failover with thinking downgrade
- Groq and Gemini providers
- Plugin system with manifest and lifecycle

## [1.0.0] - 2025-10

### Added
- Core agent loop with tool execution
- Anthropic Claude provider with OAuth support
- OpenAI/Ollama provider
- Per-user conversation isolation
- Telegram adapter with streaming responses
- CLI: init, start, stop, restart, status
- File operations (read, write, list, send)
- Session logging (JSONL append-only)
- Memory system (WAL protocol, daily notes)
- Rate limiting (per-user sliding window)
- Safe file writes (system dir blocking)
