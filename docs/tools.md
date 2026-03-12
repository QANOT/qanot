# Tools

Qanot AI provides built-in tools that the agent can call during conversations. Tools are the mechanism by which the agent interacts with the file system, web, memory, and scheduling system.

## How Tools Work

The agent loop works like this:

1. The LLM sees tool definitions in its prompt
2. It responds with `tool_use` blocks specifying which tool to call and with what parameters
3. Qanot executes the tool and returns the result
4. The LLM processes the result and either calls more tools or responds to the user

Each tool execution has a 30-second timeout. Results exceeding 8,000 characters are truncated (70% head, 20% tail).

## Built-in Tools

### read_file

Read a file from the workspace or an absolute path.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | File path (relative to workspace or absolute) |

```json
{"path": "notes/todo.md"}
```

Returns the file content as text. Files exceeding 10,000 characters are truncated with a note showing total size.

### write_file

Write content to a file, creating parent directories as needed.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | File path |
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

Execute a sandboxed command in the workspace directory.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `command` | string | Yes | Command to execute |

```json
{"command": "python3 script.py"}
```

**Security:** Only allowlisted commands can be executed:

`python3`, `python`, `curl`, `ffmpeg`, `zip`, `unzip`, `git`, `ls`, `cat`, `head`, `tail`, `grep`, `wc`, `pip`, `pip3`

Commands time out after 30 seconds. Output is capped at 10,000 characters.

### web_search

Search the internet using DuckDuckGo's instant answer API.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | Yes | Search query |

```json
{"query": "Python asyncio tutorial"}
```

Returns JSON with abstract, answer, and related topics (up to 5). This is a lightweight search -- it returns DuckDuckGo instant answers, not full web page content.

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

Get current session statistics.

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
  "last_prompt_tokens": 45000
}
```

## Cron Tools

These tools let the agent create and manage scheduled jobs. See [Scheduler](scheduler.md) for details on how cron jobs execute.

### cron_create

Create a new scheduled job.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Unique job name |
| `schedule` | string | Yes | Cron expression (e.g., `0 */4 * * *`) |
| `prompt` | string | Yes | Instructions for the agent when the job runs |
| `mode` | string | No | `isolated` (default) or `systemEvent` |

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

Returns the full jobs.json content.

### cron_update

Update an existing job.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Job name to update |
| `schedule` | string | No | New cron expression |
| `mode` | string | No | New execution mode |
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

Index a file into the RAG system.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | File path (.txt, .md, .csv, .pdf) |
| `name` | string | No | Display name (default: filename) |

### rag_search

Search indexed documents with hybrid semantic + keyword search.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | Yes | Search query |
| `top_k` | int | No | Number of results (default: 5) |

### rag_list

List all indexed document sources with chunk counts.

### rag_forget

Remove a document source from the RAG index.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source` | string | Yes | Source name to remove |

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

- **Command allowlist:** `run_command` only executes allowlisted binaries. Arbitrary shell commands are blocked.
- **Timeout:** All tools time out after 30 seconds.
- **Result truncation:** Oversized results are truncated to 8,000 characters to prevent context bloat.
- **Loop detection:** The agent loop detects repeated identical tool calls (3 consecutive or alternating patterns) and breaks the loop with a message to the user.
- **Deterministic error hints:** Tool errors containing patterns like "not found" or "permission denied" get a hint telling the LLM not to retry with the same parameters.
