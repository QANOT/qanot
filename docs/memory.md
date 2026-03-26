# Memory System

Qanot AI has a three-tier memory system: session state (short-term), daily notes (medium-term), and MEMORY.md (long-term). The WAL Protocol ties them together by scanning every user message before the agent responds.

## WAL Protocol (Write-Ahead Logging)

The core idea: before the agent generates a response, every user message is scanned for important information and written to `SESSION-STATE.md`. This ensures corrections, preferences, and decisions are captured even if the conversation is later compacted or lost.

### How It Works

1. User sends a message
2. `wal_scan()` runs regex patterns against the message
3. Matching entries are appended to `SESSION-STATE.md` with timestamps
4. Only then does the agent process the message and respond

### What Gets Captured

| Category | Trigger Pattern | Example |
|----------|----------------|---------|
| `correction` | "actually", "no I meant", "it's not X, it's Y" | "Actually, my name is Sardor, not Sarvar" |
| `proper_noun` | "my name is", "I'm", "call me" + capitalized word | "My name is Bobur" |
| `preference` | "I like", "I prefer", "I don't like", "I want" | "I prefer dark mode" |
| `decision` | "let's do", "go with", "use" | "Let's go with PostgreSQL" |
| `specific_value` | Dates, URLs, large numbers | "The deadline is 2025-06-15" |
| `remember` | "remember this", "don't forget", "eslab qol", "unutma", "yodda tut" | "Remember that the API key rotates monthly" |

### Durable Categories

Entries in the categories `proper_noun`, `preference`, and `remember` are automatically saved to `MEMORY.md` in addition to `SESSION-STATE.md`. These are considered durable facts that should persist beyond the current session. Duplicate detection prevents the same fact from being written twice.

### SESSION-STATE.md Format

```markdown
# SESSION-STATE.md -- Active Working Memory

- [2025-01-15T10:30:00+00:00] **proper_noun**: My name is Sardor
- [2025-01-15T10:31:00+00:00] **preference**: I prefer Python over JavaScript
- [2025-01-15T10:35:00+00:00] **decision**: let's use FastAPI for the backend
```

This file is included in the system prompt, so the agent always has access to the latest session context.

## Daily Notes

Every conversation exchange is summarized and appended to a daily note file at `workspace/memory/YYYY-MM-DD.md`.

```markdown
# Daily Notes -- 2025-01-15

## [10:30:00]
**User:** Tell me about FastAPI...
**Agent:** FastAPI is a modern Python web framework...

## [10:35:00]
**User:** How do I set up authentication?...
**Agent:** For JWT authentication with FastAPI...
```

Daily notes serve as medium-term memory. The `memory_search` tool searches across the last 30 daily notes. When RAG is enabled, daily notes are also indexed for semantic search.

## MEMORY.md (Long-Term Memory)

`workspace/MEMORY.md` is the long-term memory file. The agent writes important facts, user preferences, and project context here. Unlike daily notes, which are date-scoped, MEMORY.md is persistent and manually curated by the agent.

The agent decides what to write to MEMORY.md based on its SOUL.md instructions. Typical entries include:

- User preferences and communication style
- Project context and architecture decisions
- Recurring patterns and learned behaviors

## Memory Search

The `memory_search` tool searches across all three memory tiers:

1. **MEMORY.md** -- long-term facts
2. **Daily notes** -- last 30 days of conversation summaries
3. **SESSION-STATE.md** -- current session WAL entries

Search is case-insensitive substring matching. When RAG is enabled, the search is upgraded to use semantic vector search with BM25 hybrid ranking (see [RAG](rag.md)).

```python
# Agent calls memory_search with query
results = memory_search("FastAPI authentication", workspace_dir)
# Returns: [{"file": "memory/2025-01-15.md", "line": 12, "content": "..."}]
```

## Context Management and Compaction

As conversations grow, the context window fills up. Qanot tracks token usage and takes action at specific thresholds.

### Working Buffer (50% Threshold)

When context usage reaches 50%, the Working Buffer activates:

- A `working-buffer.md` file is created in the memory directory
- Every exchange (user message + agent summary) is appended to this file
- This serves as a backup in case compaction loses important context

```markdown
# Working Buffer (Danger Zone Log)
**Status:** ACTIVE
**Started:** 2025-01-15T14:30:00+00:00

---

## [2025-01-15 14:30:00] Human
Can you refactor the database module?

## [2025-01-15 14:30:00] Agent (summary)
Refactored the database module to use connection pooling...
```

### Proactive Compaction (60% Threshold)

When the estimated next-turn context would exceed 60% of the max:

1. The first 2 messages (initial context) are kept
2. The last 4 messages (recent context) are kept
3. Everything in between is removed
4. A summary marker is inserted explaining what happened

```
[CONTEXT COMPACTION: 12 earlier messages were removed to free context space.
Recent conversation preserved below. Check your workspace files
(SESSION-STATE.md, memory/) for any important context from earlier.]
```

After compaction, the token estimate is adjusted to approximately 35% of max.

### Compaction Recovery

If the agent detects signs of compaction (truncation markers, "where were we?" messages), it automatically injects recovery context from:

1. Working buffer contents
2. SESSION-STATE.md entries
3. Today's daily notes

This recovery is appended to the user's message so the agent can re-orient without losing critical context.

### Tool Result Truncation

Tool results exceeding 8,000 characters are truncated to prevent context bloat. The truncation keeps 70% from the beginning and 20% from the end with a marker showing how many characters were removed.

## Memory Write Hooks

When memory is written (WAL entries, daily notes), registered hooks are notified. The RAG system uses this to automatically re-index memory content:

```python
# Internal hook registration (done automatically in main.py)
def on_memory_write(content: str, source: str) -> None:
    asyncio.create_task(rag_indexer.index_text(content, source=source))

add_write_hook(on_memory_write)
```

This means RAG search results include the latest memory entries without manual re-indexing.

## Anthropic Memory Tool

Qanot AI v2.0.4 adds Anthropic's trained memory tool (`memory_20250818`) with a dual-layer architecture.

### Dual-Layer Architecture

The memory system operates on two layers:

1. **All providers** get the `/memories` tool -- a file-based memory system in `workspace/memories/` with operations: view, create, str_replace, insert, delete, rename. This works with any LLM provider.
2. **Anthropic** gets the `memory_20250818` type hint, which activates trained memory behavior. The model automatically checks memories at conversation start and creates structured notes without explicit instructions.

### /memories Directory

Memory entries are stored as files in `workspace/memories/`:

```
workspace/
  memories/
    user-preferences.md
    project-context.md
    api-endpoints.md
```

The `/memories` directory is automatically indexed by RAG alongside MEMORY.md and daily notes, making all memory entries searchable via semantic search.

### Configuration

```json
{
  "memory_tool": true
}
```

When enabled:
- The `memory` tool is registered with all six operations (view, create, str_replace, insert, delete, rename)
- The `workspace/memories/` directory is created if it does not exist
- RAG indexes the `/memories` directory on startup and on changes
- For Anthropic providers, the `memory_20250818` type hint is included in tool definitions

## File Locations

| File | Purpose | Included in System Prompt |
|------|---------|--------------------------|
| `workspace/SESSION-STATE.md` | WAL entries for current session | Yes |
| `workspace/MEMORY.md` | Long-term memory | Yes (injected as "Your Long-Term Memory" section) |
| `workspace/memory/YYYY-MM-DD.md` | Daily conversation notes | No (searched on demand) |
| `workspace/memory/working-buffer.md` | Danger zone backup log | Only during compaction recovery |
