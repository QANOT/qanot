# TOOLS.md - Tool Configuration & Notes

> Document tool-specific configurations, gotchas, and credentials here.

---

## Built-in Tools

**Status:** All working

### File Operations
- `read_file` — Read a file from workspace
- `write_file` — Write/create a file
- `list_files` — List directory contents

### System
- `run_command` — Run sandboxed shell commands (python3, curl, ffmpeg, zip, git, pip)
- `send_file` — Send a file to the user via Telegram

### Web
- `web_search` — Search the web via Brave Search API
- `web_fetch` — Fetch and parse a URL (SSRF protected)

### Memory
- `memory` — Persistent /memories directory (view, create, edit, delete, rename)
- `memory_search` — Search across memory files (RAG hybrid search)

### Session & Cost
- `session_status` — Check context usage, token count
- `cost_status` — Per-user token and cost statistics

### Scheduling
- `cron_create` — Create a scheduled job
- `cron_list` — List all scheduled jobs
- `cron_update` — Update a scheduled job
- `cron_delete` — Delete a scheduled job

### Skills
- `create_skill` — Create a reusable skill (SKILL.md + script)
- `list_skills` — List all available skills
- `run_skill_script` — Execute a skill script
- `delete_skill` — Delete a skill

### Documents
- `create_docx` / `read_docx` / `edit_docx` — Word documents
- `create_xlsx` / `read_xlsx` / `edit_xlsx` — Excel spreadsheets
- `create_pdf` / `read_pdf` / `edit_pdf` — PDF documents
- `create_pptx` / `read_pptx` / `edit_pptx` — PowerPoint presentations

### Image
- `generate_image` — Generate images (Gemini / Nano Banana)
- `edit_image` — Edit images with AI

### Multi-Agent
- `spawn_agent` — Spawn a sub-agent (sync/async/conversation modes)
- `list_agents` — List available agents and active runs
- `cancel_agent` — Cancel a running agent
- `view_board` / `clear_board` — Shared project board
- `agent_history` — Past agent results

**When to spawn vs do it yourself:**
- 1-2 tool calls → do it yourself (web_search, read_file, etc.)
- 3+ independent tool calls or parallel workstreams → spawn agent
- Never spawn an agent just to call web_search once

### Diagnostics
- `doctor` — System health check

### O'zbekiston Biznes Toollar
- `currency_rate` — CBU rasmiy valyuta kurslari (USD, EUR, RUB...)
- `ikpu_search` — IKPU (MXIK) tovar klassifikator kodini qidirish
- `payment_link` — Click/Payme to'lov havolasi yaratish
- `tax_calculator` — QQS, aylanma soliq, ustama, nasiya kalkulyatori
- `generate_document` — Rasmiy biznes hujjat yaratish (20 tur)
- `weather` — Ob-havo ma'lumoti

---

## Optional Tools (pip install extras)

### MCP (`pip install qanot[mcp]`)
Connect to external MCP servers — tools appear automatically.

### Browser (`pip install qanot[browser]`)
- `browse_url` — Open a URL in headless browser
- `click_element` — Click an element on the page
- `fill_form` — Fill form fields
- `screenshot` — Take a screenshot
- `extract_data` — Extract structured data from page

---

## What Goes Here

- Tool configurations and settings
- Credential locations (not the credentials themselves!)
- Gotchas and workarounds discovered
- Common commands and patterns
- Integration notes

---

*Add whatever helps you do your job. This is your cheat sheet.*
