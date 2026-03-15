# RAG (Retrieval-Augmented Generation)

Qanot AI includes a built-in RAG system for indexing documents and retrieving relevant content during conversations. It uses hybrid search combining vector similarity with BM25 keyword matching.

## Overview

RAG lets the agent search through documents that are too large to fit in the context window. Instead of including entire files in the system prompt, documents are chunked, embedded, and stored in a local SQLite database. When the agent needs information, it searches semantically.

**When to use RAG:**
- The bot needs to reference large documents (knowledge bases, manuals, logs)
- You want the agent to remember and search across past conversations
- You need semantic search (meaning-based) rather than just keyword matching

**When RAG is not needed:**
- The bot only has short conversations with no document reference
- All necessary context fits in the system prompt (SOUL.md, TOOLS.md)
- You are using a provider without embedding support and cannot add one

## Setup

### Requirements

1. Install the RAG dependency:

```bash
pip install qanot[rag]
```

This installs `sqlite-vec`, the SQLite extension for vector operations.

2. Have a Gemini or OpenAI provider configured. Anthropic and Groq do not offer embedding APIs.

3. Enable RAG in config (enabled by default):

```json
{
  "rag_enabled": true
}
```

### Embedding Provider Auto-Detection

Qanot automatically selects the best available embedding provider from your existing config. No extra API keys or configuration needed.

**Priority order:**

| Priority | Provider | Model | Dimensions | Cost |
|----------|----------|-------|------------|------|
| 0 | FastEmbed | `nomic-ai/nomic-embed-text-v1.5` | 768 | Free (CPU, ONNX) |
| 1 | Gemini | `gemini-embedding-001` | 3072 | Free tier |
| 2 | OpenAI | `text-embedding-3-small` | 1536 | $0.02/MTok |

FastEmbed is automatically selected when an Ollama provider is detected. It runs on CPU via ONNX runtime, avoiding GPU VRAM conflicts with the chat model. Install with `pip install fastembed`.

The embedder checks both multi-provider configs and single-provider config. If you have a Gemini provider for failover, its API key will be reused for embeddings.

**Example:** If your config has Anthropic as the primary provider and Gemini as a failover, RAG will use Gemini for embeddings:

```json
{
  "providers": [
    {"name": "main", "provider": "anthropic", "model": "claude-sonnet-4-6", "api_key": "sk-ant-..."},
    {"name": "backup", "provider": "gemini", "model": "gemini-2.5-flash", "api_key": "AIza..."}
  ]
}
```

Result: Chat uses Anthropic, embeddings use Gemini (free).

If no compatible embedding provider is found, RAG is silently disabled with a log warning.

## How It Works

### Indexing Pipeline

```
Document text
    |
    v
chunk_text() -- Split into ~512-token chunks with 64-token overlap
    |
    v
Embedder.embed() -- Convert chunks to vectors (batches of 100)
    |
    v
SqliteVecStore.add() -- Store vectors + metadata in SQLite
    |
    v
BM25Index.add() -- Index chunk text for keyword search
```

**Chunking strategy:**
1. Split by double newlines (paragraphs)
2. If a paragraph exceeds the chunk size, split by sentences
3. If a sentence still exceeds, split by words
4. Merge small segments with overlap between chunks

Token estimation uses 1 token = 4 characters.

### Search Pipeline

```
Query text
    |
    +-- Embedder.embed_single() --> Vector similarity search (sqlite-vec)
    |                                    |
    +-- BM25Index.search() -----------> Keyword matching
    |                                    |
    v                                    v
         Reciprocal Rank Fusion (RRF)
              |
              v
         Ranked results (top_k)
```

**Hybrid search** combines both signals using weighted reciprocal rank fusion:
- Vector weight: 70% (semantic meaning)
- BM25 weight: 30% (exact keyword matching)
- RRF constant k=60

This hybrid approach handles cases where semantic search misses exact terms and keyword search misses paraphrased concepts.

### Storage

RAG uses SQLite with the `sqlite-vec` extension. The database is stored at `workspace_dir/rag.db`.

Schema:
- `chunks` table: text content, source identifier, user_id, metadata JSON, timestamp
- `chunks_vec` virtual table: float32 vectors for similarity search
- Indexes on `source` and `user_id` for filtered queries

## RAG Tools

The agent has four RAG tools available during conversation:

### rag_index

Index a file into the RAG system.

```
Tool: rag_index
Input: {"path": "report.md", "name": "Q4 Report"}
```

- Supports `.txt`, `.md`, `.csv`, `.pdf` files
- Path is relative to workspace or absolute
- Re-indexing a source deletes old chunks first
- Returns chunk count

### rag_search

Search indexed documents.

```
Tool: rag_search
Input: {"query": "quarterly revenue figures", "top_k": 5}
```

- Returns ranked results with text, source, and score
- Results include both vector matches and keyword matches
- Filtered by user_id when called from a conversation

### rag_list

List all indexed document sources.

```
Tool: rag_list
Input: {}
```

Returns source names, chunk counts, and indexing timestamps.

### rag_forget

Remove a document from the index.

```
Tool: rag_forget
Input: {"source": "Q4 Report"}
```

Deletes all chunks for the given source and clears the BM25 index.

## Memory Integration

RAG automatically indexes the agent's memory files:

1. **On startup:** `index_workspace()` indexes MEMORY.md, SESSION-STATE.md, and the last 30 daily notes
2. **On memory writes:** A write hook triggers re-indexing when WAL entries or daily notes are written
3. **Content-hash deduplication:** Files are only re-indexed when their content changes

The `memory_search` built-in tool checks RAG first (when available) before falling back to substring search:

```python
# memory_search tool behavior:
if rag_indexer is not None:
    results = await rag_indexer.search(query)  # Semantic search
    if results:
        return results
# Fallback: substring search across files
results = memory_search(query, workspace_dir)
```

## RAG Modes

Qanot supports three RAG modes to handle different model capabilities:

```json
{
  "rag_mode": "auto"
}
```

| Mode | Behavior | Best For |
|------|----------|----------|
| `"auto"` (default) | Auto-injects top 3 memory hints into every message + keeps `rag_search` tool for deeper queries | All models — works even with small/cheap models |
| `"agentic"` | No auto-injection. Agent uses `rag_search` tool when it decides to | Smart models (Claude, GPT-4) that reliably use tools |
| `"always"` | Same as `auto` — always injects context hints | When you want guaranteed context regardless of model |

### Why This Matters

Not all models are equally capable of deciding when to search:

| Model | Will it call `rag_search` on its own? |
|-------|--------------------------------------|
| Claude Sonnet/Opus | Yes |
| GPT-4.1 | Yes |
| Gemini Pro | Mostly |
| Llama 3.3 70B (Groq) | Unreliable |
| Llama 3.1 8B (Groq) | Almost never |

In `"auto"` mode, every user message (longer than 10 chars) triggers a lightweight semantic search against memory. The top 3 results are appended as `[MEMORY CONTEXT]` hints. This costs one embedding API call per message but ensures even small models have relevant context.

In `"agentic"` mode, nothing is injected — the model must decide to call `rag_search` as a tool. Use this only with smart models to save embedding costs.

### How Auto-Injection Works

```
User: "What was the API endpoint we discussed?"
                    ↓
_prepare_turn() — WAL scan
                    ↓
RAG search: top 3 memory hits
                    ↓
Message becomes:
  "What was the API endpoint we discussed?
   ---
   [MEMORY CONTEXT — relevant past information]
   - [memory/2026-03-10.md] Discussed API endpoint /v2/users for...
   - [SESSION-STATE.md] Decision: use REST API with /v2 prefix..."
                    ↓
LLM responds with context (even dumb models get it right)
```

The `rag_search` tool is still available for explicit deeper searches beyond the 3 auto-injected hints.

## Configuration

RAG behavior is controlled by config and source constants:

| Setting | Value | Location |
|---------|-------|----------|
| RAG mode | `"auto"` / `"agentic"` / `"always"` | `config.json: rag_mode` |
| Chunk size | 512 tokens (~2048 chars) | `RAGEngine.chunk_size` |
| Chunk overlap | 64 tokens (~256 chars) | `RAGEngine.chunk_overlap` |
| BM25 weight | 0.3 (30%) | `RAGEngine.bm25_weight` |
| Auto-inject count | 3 results | `agent.py: _prepare_turn()` |
| Min message length | 10 chars (skips "hi", "ok") | `agent.py: _prepare_turn()` |
| Embedding batch size | 100 texts per API call | `Embedder.embed()` |
| Supported file types | `.txt`, `.md`, `.csv`, `.pdf` | `tools/rag.py` |
| Max daily notes indexed | 30 most recent | `MemoryIndexer` |

## Limitations

- **Embedding providers:** Only Gemini and OpenAI support embeddings. Anthropic and Groq do not.
- **sqlite-vec required:** Without `pip install sqlite-vec`, vector search is disabled. The metadata table still works, but similarity search returns empty results.
- **In-memory BM25:** The BM25 index is rebuilt from scratch on startup and after source deletion. It does not persist to disk.
- **PDF requires PyMuPDF:** PDF parsing uses PyMuPDF (`pip install PyMuPDF`), included in `pip install qanot[rag]`. Without it, PDF indexing returns an error with install instructions.
- **Single-user scope:** Results are filtered by user_id, but the vector store is shared across all users. In multi-user setups, all users' documents share the same embedding space.
