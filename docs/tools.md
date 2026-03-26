# Tools

Qanot AI provides built-in tools that the agent can call during conversations. Tools are the mechanism by which the agent interacts with the file system, web, memory, scheduling, RAG, image generation, multi-agent delegation, and diagnostics.

## How Tools Work

The agent loop works like this:

1. The LLM sees tool definitions in its prompt
2. It responds with `tool_use` blocks specifying which tool to call and with what parameters
3. Qanot executes the tool and returns the result
4. The LLM processes the result and either calls more tools or responds to the user

Each tool execution has a 120-second timeout (configurable per-tool). Results exceeding 50,000 characters are truncated.

## Built-in Tools

### read_file

Read a file from the workspace or an absolute path.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | File path (relative to workspace or absolute) |

```json
{"path": "notes/todo.md"}
```

Returns the file content as text. Files exceeding 50,000 characters are truncated with a note showing total size.

### write_file

Write content to a file, creating parent directories as needed. Paths are validated by `fs_safe.validate_write_path()` to block writes to system directories and symlink attacks.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | File path (relative to workspace or absolute) |
| `content` | string | Yes | File content |

```json
{"path": "notes/todo.md", "content": "# TODO\n\n- Buy groceries"}
```

Returns `{"success": true, "path": "...", "bytes": 123}`.

### list_files

List files and directories in a given path.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | No | Directory path (default: workspace root) |

```json
{"path": "notes/"}
```

Returns a JSON array of entries with `name`, `type` ("file" or "dir"), and `size`.

### run_command

Execute a shell command in the workspace directory.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `command` | string | Yes | Shell command (pipes, redirects, `&&` supported) |
| `timeout` | integer | No | Timeout in seconds (default: 120, max: 120) |
| `cwd` | string | No | Working directory (default: workspace) |
| `approved` | boolean | No | User approval confirmation (for cautious mode) |

```json
{"command": "python3 script.py"}
```

**Security:** Uses a 3-tier security model configured via `exec_security`:

- **`open`** (default) -- Only a blocklist of dangerous patterns is enforced. Commands like `rm -rf /`, `mkfs`, `dd`, fork bombs, and attack tools are always blocked regardless of mode.
- **`cautious`** -- Dangerous patterns blocked, plus risky commands (pip install, curl, sudo, git push, docker, database clients, etc.) require user approval via inline buttons. If the user denies, the command is rejected.
- **`strict`** -- Only commands matching `exec_allowlist` (prefix match) are permitted. Everything else is blocked.

Commands time out after 120 seconds. Output is capped at 50,000 characters.

### memory_search

Search across the agent's memory files.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | Yes | Search query |

```json
{"query": "database password"}
```

When RAG is enabled, uses semantic vector search. Falls back to case-insensitive substring matching across MEMORY.md, daily notes (last 30), and SESSION-STATE.md. Results are limited to 50.

### session_status

Get current session statistics including context usage and cost.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| (none) | -- | -- | No parameters |

```json
{}
```

Returns:

```json
{
  "context_percent": 23.5,
  "total_input_tokens": 45000,
  "total_output_tokens": 12000,
  "total_tokens": 57000,
  "max_tokens": 200000,
  "buffer_active": false,
  "turn_count": 8,
  "last_prompt_tokens": 45000,
  "user_cost": {"input_tokens": 45000, "output_tokens": 12000, "cost_usd": 0.12},
  "total_cost": 1.45
}
```

### cost_status

Get token usage and cost statistics per user.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `user_id` | string | No | User ID to query (default: current user) |

```json
{"user_id": "123456"}
```

Returns per-user token counts, cost breakdown, and totals. When `user_id` is omitted, returns stats for the current user. Cost tracking must be enabled.

### send_file

Send a file from the workspace to the user via Telegram.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | File path (relative to workspace or absolute) |

```json
{"path": "generated/report.pdf"}
```

Returns `{"success": true, "path": "...", "size": 12345}`. Files are queued for delivery by the Telegram adapter. Maximum file size is 50 MB (Telegram limit).

## Web Tools

### web_search

Search the internet using the Brave Search API.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | Yes | Search query (max 2000 characters) |
| `count` | integer | No | Number of results (1-10, default: 5) |

```json
{"query": "Python asyncio tutorial", "count": 5}
```

Returns JSON with query, result count, and an array of results each containing `title`, `url`, `description`, and optionally `age`. Results are cached for 15 minutes (up to 50 entries). Requires `brave_api_key` in config.

### web_fetch

Fetch and extract readable content from a web page URL.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `url` | string | Yes | URL to fetch (http:// or https://) |
| `max_chars` | integer | No | Max output characters (default: 50,000) |

```json
{"url": "https://docs.python.org/3/library/asyncio.html"}
```

Returns JSON with `url`, `final_url`, `title`, `content` (extracted text), `content_type`, `length`, and a source disclaimer. HTML pages are converted to simplified markdown (headings, links, paragraphs). JSON responses are pretty-printed.

**SSRF Protection:** URLs are validated against:
- Blocked hostnames (localhost, metadata.google.internal)
- Blocked ports (SSH, SMTP, database ports, Docker daemon, etc.)
- Private/reserved IP networks (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, link-local, IPv6 loopback)
- DNS resolution is checked to catch hostname-to-private-IP redirects
- Redirect targets are re-validated (max 3 redirects)
- Response body is limited to 2 MB
- 30-second timeout

## Image Tools

Available when `gemini_api_key` is configured. Powered by Gemini (Nano Banana) image generation.

### generate_image

Generate a new image from a text description.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `prompt` | string | Yes | Detailed text description of the image to generate |
| `model` | string | No | Image model (default: `gemini-3-pro-image-preview`) |

Supported models:
- `gemini-3-pro-image-preview` -- Nano Banana Pro (highest quality)
- `gemini-3.1-flash-image-preview` -- Nano Banana 2 (fast)
- `gemini-2.5-flash-image` -- Nano Banana (speed optimized)

```json
{"prompt": "A serene mountain landscape at sunset with a lake reflection"}
```

Returns `{"status": "ok", "image_path": "...", "model": "...", "description": "...", "size_bytes": 123456}`. The image is saved to `workspace/generated/` and automatically sent to the user via Telegram.

### edit_image

Edit the user's last sent photo based on a text instruction.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `prompt` | string | Yes | Text instruction describing the edit (e.g., "make it black and white") |
| `model` | string | No | Image model (same options as generate_image) |

```json
{"prompt": "Remove the background and replace with mountains"}
```

The tool searches backwards through the conversation to find the last user-sent image. If no image is found, returns an error asking the user to send a photo first. The edited image is saved and sent to the user.

## Cron Tools

These tools let the agent create and manage scheduled jobs. See [Scheduler](scheduler.md) for details on how cron jobs execute.

### cron_create

Create a new scheduled job or one-shot reminder.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Unique job name (max 200 chars) |
| `schedule` | string | No* | Cron expression (e.g., `0 */4 * * *`) |
| `at` | string | No* | ISO 8601 timestamp for one-shot reminder (e.g., `2026-03-12T17:00:00+05:00`) |
| `prompt` | string | Yes | Reminder text or task prompt (max 10,000 chars) |
| `mode` | string | No | `isolated` (full agent with tools) or `systemEvent` (text delivery only). Default: `systemEvent` |
| `delete_after_run` | boolean | No | Auto-delete after execution (default: true for `at` reminders) |
| `timezone` | string | No | IANA timezone (e.g., `Asia/Tashkent`) |

*Either `schedule` or `at` is required.

```json
{
  "name": "daily-summary",
  "schedule": "0 20 * * *",
  "prompt": "Write a summary of today's conversations and save to MEMORY.md",
  "mode": "isolated"
}
```

The scheduler reloads automatically after creation.

### cron_list

List all scheduled jobs.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| (none) | -- | -- | No parameters |

Returns the full jobs.json content.

### cron_update

Update an existing job.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Job name to update |
| `schedule` | string | No | New cron expression |
| `mode` | string | No | New execution mode (`systemEvent` or `isolated`) |
| `prompt` | string | No | New prompt |
| `enabled` | boolean | No | Enable/disable |

### cron_delete

Delete a scheduled job.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Job name to delete |

## RAG Tools

Available when `rag_enabled: true` and a compatible embedding provider exists. See [RAG](rag.md) for the full documentation.

### rag_index

Index a file into the RAG system. Re-indexing the same file replaces existing chunks.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | File path (.txt, .md, .csv, .pdf) -- must be within workspace |
| `name` | string | No | Display name / source identifier (default: filename) |

```json
{"path": "docs/handbook.pdf", "name": "Employee Handbook"}
```

Returns `{"indexed": true, "source": "Employee Handbook", "chunks": 42}`. PDF support requires PyMuPDF (`pip install PyMuPDF`). Paths are validated to prevent directory traversal outside workspace.

### rag_search

Search indexed documents with hybrid semantic + keyword search.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | Yes | Search query (max 10,000 characters) |
| `top_k` | integer | No | Number of results (1-100, default: 5) |

```json
{"query": "vacation policy", "top_k": 3}
```

Returns an array of results with `text`, `source`, and `score` (0-1, higher is better).

### rag_list

List all indexed document sources with chunk counts.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| (none) | -- | -- | No parameters |

### rag_forget

Remove a document source from the RAG index.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source` | string | Yes | Source name to remove |

```json
{"source": "Employee Handbook"}
```

Returns `{"deleted": true, "source": "...", "chunks_removed": 42}`.

## Delegation Tools

Multi-agent collaboration tools. The main agent can delegate tasks to specialized agents, have multi-turn conversations with them, and share results via a project board.

### delegate_to_agent

One-shot task delegation -- hand off a task to another agent and wait for the result.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task` | string | Yes | Task description (detailed) |
| `agent_id` | string | Yes | Target agent identifier |
| `context` | string | No | Optional context relevant to the task (max 4,000 chars) |

```json
{
  "task": "Write SEO-optimized meta descriptions for these 5 product pages",
  "agent_id": "seo-expert",
  "context": "Target market is Uzbekistan, write in Uzbek language"
}
```

Returns the agent's result (max 8,000 chars). Has a 120-second timeout. Maximum delegation depth is 2 (agents can delegate to other agents, but not infinitely). Loop detection prevents circular delegations. Results are posted to the shared project board.

### converse_with_agent

Multi-turn ping-pong conversation with another agent (up to 5 turns).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message` | string | Yes | Message to send to the agent |
| `agent_id` | string | Yes | Target agent identifier |
| `max_turns` | integer | No | Maximum conversation turns (1-5, default: 3) |

```json
{
  "message": "Let's design the database schema for a blog platform",
  "agent_id": "architect",
  "max_turns": 5
}
```

Returns the full conversation transcript. Useful for collaborative problem-solving and negotiations between agents.

### view_project_board

View the shared project board -- see results from all agents' completed work.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_id` | string | No | Filter by agent ID |

```json
{"agent_id": "seo-expert"}
```

Returns an array of board entries with `agent_id`, `task`, `result`, and `timestamp`. Max 20 entries per user. Board data is evicted after 6 hours of inactivity.

### clear_project_board

Clear the shared project board.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| (none) | -- | -- | No parameters |

### list_agents

List all available agents with their model, role, and capabilities.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| (none) | -- | -- | No parameters |

Returns an array of agent definitions including `id`, `name`, `model`, `prompt` (truncated), and `tools_allow`/`tools_deny`.

### agent_session_history

Read another agent's conversation transcript.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_id` | string | Yes | Agent whose history to read |

Returns the last 20 messages from the agent's session, including role, content, timestamp, and whether tools were used.

### agent_sessions_list

List all active agent sessions with metadata.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| (none) | -- | -- | No parameters |

Returns session info for each active agent: message count, last activity timestamp.

### view_agent_activity

View the agent activity log -- real-time monitoring of all agent interactions.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `limit` | integer | No | Max entries to return (default: 20, max: 50) |
| `agent_id` | string | No | Filter by agent ID |

Returns a feed of delegation events, results, errors, and timing data.

### set_monitor_group

Configure a Telegram group for real-time agent monitoring.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `group_id` | integer | Yes | Telegram group ID (negative number, e.g., -1001234567890) |

When set, agent-to-agent interactions are forwarded to this group so you can watch them in real time. Each agent bot must be added to the group.

## Sub-Agent Tools

### spawn_sub_agent

Spawn an isolated background sub-agent for complex, long-running tasks. The sub-agent works independently and delivers results to the user via Telegram when finished.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task` | string | Yes | Task description -- detailed and self-contained (max 10,000 chars) |

```json
{"task": "Research Python vs Rust performance benchmarks -- use web_search to find recent comparisons and summarize findings with sources"}
```

Returns `{"status": "spawned", "task_id": "abc12345", "message": "..."}`. The sub-agent has a 5-minute timeout. Maximum 3 concurrent sub-agents per user. Available tools: web_search, web_fetch, read_file, memory_search.

### list_sub_agents

List active sub-agents for the current user.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| (none) | -- | -- | No parameters |

Returns `{"active": 2, "agents": [{"task_id": "abc12345", "status": "running"}, ...]}`.

## Agent Manager Tools

Dynamic agent lifecycle management -- create, update, and delete agents at runtime without restarting the bot.

### create_agent

Create a new agent with its own personality, model, and optionally its own Telegram bot.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | string | Yes | Agent ID (lowercase alphanumeric + hyphens, max 32 chars) |
| `name` | string | Yes | Agent display name |
| `prompt` | string | No | Agent personality and instructions (auto-generated if omitted) |
| `model` | string | No | LLM model (default: main agent's model) |
| `provider` | string | No | LLM provider (default: main agent's provider) |
| `bot_token` | string | No | Telegram bot token -- if provided, launches as a standalone Telegram bot |
| `tools_allow` | array[string] | No | Allowlist of tools (empty = all tools) |
| `tools_deny` | array[string] | No | Denylist of tools |
| `timeout` | integer | No | Timeout in seconds (default: 120) |

```json
{
  "id": "seo-expert",
  "name": "SEO Mutaxassis",
  "prompt": "You are an SEO expert specializing in the Uzbekistan market...",
  "model": "claude-haiku-4-5"
}
```

Agents are persisted to config.json and hot-launched without restart. If `bot_token` is provided, the agent runs as an independent Telegram bot. Without a token, it is an internal agent accessible via `delegate_to_agent`. A SOUL.md file is created in `workspace/agents/<id>/`.

### update_agent

Update an existing agent's configuration. Only provide fields you want to change.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | string | Yes | Agent ID to update |
| `name` | string | No | New display name |
| `prompt` | string | No | New personality/instructions (also updates SOUL.md) |
| `model` | string | No | New LLM model |
| `provider` | string | No | New LLM provider |
| `bot_token` | string | No | New Telegram bot token (empty string removes token and stops bot) |
| `tools_allow` | array[string] | No | New tool allowlist |
| `tools_deny` | array[string] | No | New tool denylist |
| `timeout` | integer | No | New timeout |

```json
{"id": "seo-expert", "model": "claude-opus-4-6"}
```

Returns `{"status": "updated", "agent_id": "...", "changes": ["model"]}`.

### delete_agent

Delete an agent. If it has a running Telegram bot, the bot is stopped.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | string | Yes | Agent ID to delete |

```json
{"id": "seo-expert"}
```

Returns `{"status": "deleted", "agent_id": "...", "bot_stopped": true}`.

### restart_self

Restart the entire bot process. Useful after configuration changes, new agent creation, or error recovery. The bot sends SIGTERM to itself and relies on the service manager (systemd/launchd) to respawn it.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `reason` | string | No | Reason for restart |

```json
{"reason": "Applied new configuration"}
```

Returns immediately. The bot restarts after a 2-second delay.

## Memory Tool

Available when `memory_tool: true` is set in config. Provides file-based memory management in the `workspace/memories/` directory. For Anthropic, this uses the `memory_20250818` type hint for trained memory behavior.

### memory

Manage memory entries in the `/memories` directory.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `command` | string | Yes | Operation: `view`, `create`, `str_replace`, `insert`, `delete`, `rename` |
| `path` | string | No | Memory file path (relative to memories directory) |
| `content` | string | No | Content for create/insert operations |
| `old_str` | string | No | String to find (for str_replace) |
| `new_str` | string | No | Replacement string (for str_replace) |
| `insert_line` | integer | No | Line number for insert operation |
| `new_path` | string | No | New path for rename operation |

**Operations:**

| Command | Description |
|---------|-------------|
| `view` | List all memory entries, or read a specific entry if `path` is given |
| `create` | Create a new memory entry with the given `path` and `content` |
| `str_replace` | Replace `old_str` with `new_str` in the memory entry at `path` |
| `insert` | Insert `content` at `insert_line` in the memory entry at `path` |
| `delete` | Delete the memory entry at `path` |
| `rename` | Rename the memory entry from `path` to `new_path` |

```json
{"command": "create", "path": "user-preferences.md", "content": "# Preferences\n\n- Prefers dark mode\n- Language: Uzbek"}
```

## Browser Tools

Available when `browser_enabled: true` and `pip install qanot[browser]` is installed. Uses Playwright for real browser automation.

### browse_url

Open a URL in a headless browser and return the page content.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `url` | string | Yes | URL to open |
| `wait_for` | string | No | CSS selector to wait for before returning content |

```json
{"url": "https://example.com", "wait_for": ".main-content"}
```

### click_element

Click an element on the current page.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `selector` | string | Yes | CSS selector of the element to click |

```json
{"selector": "button.submit"}
```

### fill_form

Fill form fields on the current page.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `fields` | object | Yes | Map of CSS selectors to values |

```json
{"fields": {"#email": "user@example.com", "#password": "secret123"}}
```

### screenshot

Take a screenshot of the current page.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `full_page` | boolean | No | Capture full page (default: false, viewport only) |

```json
{"full_page": true}
```

Returns `{"success": true, "path": "...", "size_bytes": 123456}`. The screenshot is saved to `workspace/generated/` and sent to the user via Telegram.

### extract_data

Extract structured data from the current page using CSS selectors.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `selectors` | object | Yes | Map of field names to CSS selectors |

```json
{"selectors": {"title": "h1", "price": ".price", "description": ".product-desc"}}
```

Returns extracted text content for each selector.

## Skill Tools

The agent can create reusable skills with scripts that can be run later.

### create_skill

Create a new skill with a name, description, and executable script.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Skill name (lowercase alphanumeric + hyphens) |
| `description` | string | Yes | What the skill does |
| `script` | string | Yes | Script content (Python or shell) |

```json
{
  "name": "seo-check",
  "description": "Check SEO metrics for a URL",
  "script": "#!/usr/bin/env python3\nimport sys\nurl = sys.argv[1]\nprint(f'Checking SEO for {url}')"
}
```

### list_skills

List all available skills with their descriptions.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| (none) | -- | -- | No parameters |

### run_skill_script

Execute a skill's script.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Skill name to run |
| `args` | string | No | Arguments to pass to the script |

```json
{"name": "seo-check", "args": "https://example.com"}
```

### delete_skill

Remove a skill and its files.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Skill name to delete |

## MCP Tools

When MCP servers are configured (`mcp_servers` in config), their tools are dynamically registered at startup. MCP tools are prefixed with the server name to avoid collisions (e.g., `github_create_issue`, `filesystem_read_file`).

Use the `/mcp` Telegram command to see all connected servers and their available tools.

## Doctor Tool

### doctor

Run comprehensive system health diagnostics. Checks 7 subsystems and reports status (ok/warning/error) for each.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| (none) | -- | -- | No parameters |

```json
{}
```

Checks performed:
- **config** -- bot_token, API keys, workspace_dir writability, sessions_dir, required workspace files (SOUL.md, TOOLS.md, IDENTITY.md)
- **memory** -- MEMORY.md readability and size, SESSION-STATE.md size (warns if >100KB), daily notes count (30 days), memory/ directory size
- **context** -- Current context usage %, token counts, buffer status, compaction mode
- **provider** -- Single vs multi-provider mode, model names
- **rag** -- RAG database existence and size, FTS5 availability, embedding cache entries
- **sessions** -- Sessions directory size, recent session file count (7 days), latest session timestamp
- **disk** -- Workspace size, available disk space (warns if <100MB)

Returns:

```json
{
  "status": "healthy",
  "checks": {
    "config": {"status": "ok", "details": "..."},
    "memory": {"status": "ok", "details": "..."},
    "context": {"status": "ok", "details": "..."},
    "provider": {"status": "ok", "details": "..."},
    "rag": {"status": "ok", "details": "..."},
    "sessions": {"status": "ok", "details": "..."},
    "disk": {"status": "ok", "details": "..."}
  },
  "warnings": [],
  "timestamp": "2026-03-16T12:00:00+00:00"
}
```

## Creating Custom Tools

Custom tools are added through the [plugin system](plugins.md). For quick one-off tools, you can also register directly on the `ToolRegistry`:

```python
from qanot.agent import ToolRegistry

registry = ToolRegistry()

async def my_tool(params: dict) -> str:
    name = params.get("name", "world")
    return f"Hello, {name}!"

registry.register(
    name="greet",
    description="Greet someone by name.",
    parameters={
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "description": "Name to greet"},
        },
    },
    handler=my_tool,
)
```

Tool handlers must be async functions that accept a `dict` parameter and return a `str`. JSON is the conventional return format for structured data. Raise exceptions for errors -- they are caught and returned as `{"error": "..."}`.

## Tool Safety

- **3-tier command security:** `run_command` uses `exec_security` (open/cautious/strict) to control which commands are allowed. Dangerous patterns like `rm -rf /`, fork bombs, and attack tools are always blocked.
- **File write validation:** `write_file` validates paths with `fs_safe.validate_write_path()` -- system directories are blocked, symlinks are checked.
- **SSRF protection:** `web_fetch` validates URLs against private networks, blocked ports, and internal hostnames. DNS resolution is checked, and redirect targets are re-validated.
- **Timeout:** Command execution times out after 120 seconds. Web fetch times out after 30 seconds.
- **Result truncation:** Oversized results are truncated to 50,000 characters to prevent context bloat.
- **Loop detection:** The agent loop detects repeated identical tool calls (3 consecutive or alternating patterns) and breaks the loop with a message to the user.
- **Deterministic error hints:** Tool errors containing patterns like "not found" or "permission denied" get a hint telling the LLM not to retry with the same parameters.
- **Delegation limits:** Max depth of 2, loop detection, 120-second timeout, result size caps.
- **Sub-agent limits:** Max 3 concurrent per user, 5-minute timeout.
- **File size limits:** `send_file` enforces Telegram's 50 MB upload limit.
