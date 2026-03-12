# API Reference

This page documents the public classes and methods in Qanot AI. These are the interfaces you interact with when extending the framework or building custom integrations.

## Core Classes

### Agent

`qanot.agent.Agent`

The core agent that runs the tool_use loop. Manages per-user conversations, tool execution, and context tracking.

```python
class Agent:
    def __init__(
        self,
        config: Config,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        session: SessionWriter | None = None,
        context: ContextTracker | None = None,
        prompt_mode: str = "full",
    ): ...
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `config` | `Config` | Configuration object |
| `provider` | `LLMProvider` | LLM provider instance |
| `tool_registry` | `ToolRegistry` | Registry of available tools |
| `session` | `SessionWriter` | Session logger (created from config if None) |
| `context` | `ContextTracker` | Token tracker (created from config if None) |
| `prompt_mode` | `str` | `"full"`, `"minimal"`, or `"none"` |

**Methods:**

```python
async def run_turn(self, user_message: str, user_id: str | None = None) -> str
```

Process a user message through the agent loop. Returns the final text response. Acquires a per-user lock to prevent concurrent processing for the same user.

```python
async def run_turn_stream(
    self, user_message: str, user_id: str | None = None
) -> AsyncIterator[StreamEvent]
```

Process a user message with streaming. Yields `StreamEvent` objects as they arrive. Tool-use iterations are handled internally; text deltas from each iteration are yielded.

```python
def reset(self, user_id: str | None = None) -> None
```

Reset conversation state. If `user_id` is provided, resets only that user. If `None`, resets all users.

**Constants:**

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_ITERATIONS` | 25 | Maximum tool_use loop iterations per turn |
| `MAX_SAME_ACTION` | 3 | Break after N identical consecutive tool calls |
| `TOOL_TIMEOUT` | 30 | Seconds per tool execution |
| `CONVERSATION_TTL` | 3600 | Seconds before idle conversations are evicted |

### spawn_isolated_agent

`qanot.agent.spawn_isolated_agent`

```python
async def spawn_isolated_agent(
    config: Config,
    provider: LLMProvider,
    tool_registry: ToolRegistry,
    prompt: str,
    session_id: str | None = None,
) -> str
```

Create and run a fresh agent for a single prompt. Used by cron jobs. Returns the final response text. Uses `prompt_mode="minimal"` for smaller system prompts.

### ToolRegistry

`qanot.agent.ToolRegistry`

Registry of available tools.

```python
class ToolRegistry:
    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable[[dict], Awaitable[str]],
    ) -> None: ...

    def get_definitions(self) -> list[dict]: ...

    async def execute(
        self, name: str, input_data: dict, timeout: float = 30
    ) -> str: ...

    @property
    def tool_names(self) -> list[str]: ...
```

| Method | Description |
|--------|-------------|
| `register()` | Register a tool with its handler function |
| `get_definitions()` | Get tool definitions in LLM-compatible format |
| `execute()` | Execute a tool by name with timeout protection |
| `tool_names` | List of registered tool names |

### Config

`qanot.config.Config`

```python
@dataclass
class Config:
    bot_token: str = ""
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""
    providers: list[ProviderConfig] = field(default_factory=list)
    soul_path: str = "/data/workspace/SOUL.md"
    tools_path: str = "/data/workspace/TOOLS.md"
    plugins: list[PluginConfig] = field(default_factory=list)
    owner_name: str = ""
    bot_name: str = ""
    timezone: str = "Asia/Tashkent"
    max_concurrent: int = 4
    compaction_mode: str = "safeguard"
    workspace_dir: str = "/data/workspace"
    sessions_dir: str = "/data/sessions"
    cron_dir: str = "/data/cron"
    plugins_dir: str = "/data/plugins"
    max_context_tokens: int = 200000
    allowed_users: list[int] = field(default_factory=list)
    response_mode: str = "stream"
    stream_flush_interval: float = 0.8
    telegram_mode: str = "polling"
    webhook_url: str = ""
    webhook_port: int = 8443
    rag_enabled: bool = True
```

```python
def load_config(path: str | None = None) -> Config
```

Load configuration from a JSON file. If `path` is None, checks `QANOT_CONFIG` env var, then falls back to `/data/config.json`.

## Provider Classes

### LLMProvider

`qanot.providers.base.LLMProvider`

Abstract base class for LLM providers.

```python
class LLMProvider(ABC):
    model: str

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> ProviderResponse: ...

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]: ...
```

`chat_stream()` has a default implementation that falls back to `chat()`. Providers can override it for true streaming.

### ProviderResponse

`qanot.providers.base.ProviderResponse`

```python
@dataclass
class ProviderResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"  # "end_turn" | "tool_use"
    usage: Usage = field(default_factory=Usage)
```

### StreamEvent

`qanot.providers.base.StreamEvent`

```python
@dataclass
class StreamEvent:
    type: str       # "text_delta" | "tool_use" | "done"
    text: str = ""
    tool_call: ToolCall | None = None
    response: ProviderResponse | None = None  # set on "done"
```

### ToolCall

`qanot.providers.base.ToolCall`

```python
@dataclass
class ToolCall:
    id: str        # Provider-assigned ID
    name: str      # Tool name
    input: dict    # Tool parameters
```

### Usage

`qanot.providers.base.Usage`

```python
@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost: float = 0.0
```

### Concrete Providers

| Class | Module | Provider |
|-------|--------|----------|
| `AnthropicProvider` | `qanot.providers.anthropic` | Anthropic Claude |
| `OpenAIProvider` | `qanot.providers.openai` | OpenAI GPT |
| `GeminiProvider` | `qanot.providers.gemini` | Google Gemini |
| `GroqProvider` | `qanot.providers.groq` | Groq |
| `FailoverProvider` | `qanot.providers.failover` | Multi-provider failover wrapper |

### FailoverProvider

`qanot.providers.failover.FailoverProvider`

```python
class FailoverProvider(LLMProvider):
    def __init__(self, profiles: list[ProviderProfile]): ...

    @property
    def active_profile(self) -> ProviderProfile: ...

    def status(self) -> list[dict]: ...
```

### ProviderProfile

`qanot.providers.failover.ProviderProfile`

```python
@dataclass
class ProviderProfile:
    name: str
    provider_type: str  # "anthropic" | "openai" | "gemini" | "groq"
    api_key: str
    model: str
    base_url: str | None = None

    @property
    def is_available(self) -> bool: ...

    def mark_failed(self, error_type: str) -> None: ...
    def mark_success(self) -> None: ...
```

### Error Classification

`qanot.providers.errors`

```python
def classify_error(error: Exception) -> str
```

Returns one of: `rate_limit`, `auth`, `billing`, `overloaded`, `timeout`, `not_found`, `unknown`.

```python
PERMANENT_FAILURES = {"auth", "billing"}
TRANSIENT_FAILURES = {"rate_limit", "overloaded", "timeout", "not_found"}
```

## RAG Classes

### RAGEngine

`qanot.rag.engine.RAGEngine`

```python
class RAGEngine:
    def __init__(
        self,
        embedder: Embedder,
        store: VectorStore,
        *,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        bm25_weight: float = 0.3,
    ): ...

    async def ingest(
        self, text: str, *, source: str = "", user_id: str = "", metadata: dict | None = None,
    ) -> list[str]: ...

    async def query(
        self, query: str, *, top_k: int = 5, user_id: str | None = None, source: str | None = None,
    ) -> RAGResult: ...

    async def delete_source(self, source: str) -> int: ...

    def list_sources(self) -> list[dict]: ...
```

### RAGResult

`qanot.rag.engine.RAGResult`

```python
@dataclass
class RAGResult:
    results: list[SearchResult]
    query: str
    sources_used: list[str] = field(default_factory=list)
```

### VectorStore / SqliteVecStore

`qanot.rag.store.VectorStore` (ABC), `qanot.rag.store.SqliteVecStore`

```python
class SqliteVecStore(VectorStore):
    def __init__(self, db_path: str, dimensions: int = 768): ...

    def add(self, texts, embeddings, *, source="", user_id="", metadatas=None) -> list[str]: ...
    def search(self, query_embedding, *, top_k=5, user_id=None, source=None) -> list[SearchResult]: ...
    def delete_source(self, source: str) -> int: ...
    def list_sources(self) -> list[dict]: ...
    def close(self) -> None: ...

    # Async wrappers (inherited from VectorStore)
    async def async_add(...) -> list[str]: ...
    async def async_search(...) -> list[SearchResult]: ...
```

### SearchResult

`qanot.rag.store.SearchResult`

```python
@dataclass
class SearchResult:
    chunk_id: str
    text: str
    metadata: dict
    score: float  # 0..1, higher is better
```

### Embedder

`qanot.rag.embedder.Embedder` (ABC)

```python
class Embedder(ABC):
    dimensions: int

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_single(self, text: str) -> list[float]: ...
```

Concrete implementations: `GeminiEmbedder` (768 dims), `OpenAIEmbedder` (1536 dims).

```python
def create_embedder(config) -> Embedder | None
```

Auto-detect best embedder from config. Returns None if no compatible provider found.

### MemoryIndexer

`qanot.rag.indexer.MemoryIndexer`

```python
class MemoryIndexer:
    def __init__(self, engine: RAGEngine, workspace_dir: str = "/data/workspace"): ...

    async def index_workspace(self, user_id: str = "") -> int: ...
    async def index_text(self, text: str, *, source: str, user_id: str = "", metadata: dict | None = None) -> list[str]: ...
    async def search(self, query: str, *, top_k: int = 5, user_id: str | None = None) -> list[dict]: ...
```

### BM25Index

`qanot.rag.chunker.BM25Index`

```python
class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75): ...

    def add(self, doc_ids: list[str], texts: list[str]) -> None: ...
    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]: ...
    def clear(self) -> None: ...
```

## Plugin Classes

### Plugin

`qanot.plugins.base.Plugin`

```python
class Plugin(ABC):
    name: str = ""
    description: str = ""
    tools_md: str = ""       # Appended to workspace TOOLS.md
    soul_append: str = ""    # Appended to workspace SOUL.md

    @abstractmethod
    def get_tools(self) -> list[ToolDef]: ...

    async def setup(self, config: dict) -> None: ...
    async def teardown(self) -> None: ...
    def _collect_tools(self) -> list[ToolDef]: ...
```

### ToolDef

`qanot.plugins.base.ToolDef`

```python
@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict           # JSON Schema
    handler: Callable[[dict], Awaitable[str]]
```

### tool decorator

`qanot.plugins.base.tool`

```python
def tool(name: str, description: str, parameters: dict | None = None)
```

Decorator that marks a method as a tool. The decorated method must be async, accept `self` and `params: dict`, and return `str`.

## Utility Classes

### ContextTracker

`qanot.context.ContextTracker`

```python
class ContextTracker:
    def __init__(self, max_tokens: int = 200_000, workspace_dir: str = "/data/workspace"): ...

    @property
    def total_tokens(self) -> int: ...

    def get_context_percent(self) -> float: ...
    def add_usage(self, input_tokens: int, output_tokens: int) -> None: ...
    def needs_compaction(self) -> bool: ...
    def compact_messages(self, messages: list[dict]) -> list[dict]: ...
    def check_threshold(self) -> bool: ...
    def append_to_buffer(self, human_msg: str, agent_summary: str) -> None: ...
    def detect_compaction(self, messages: list[dict]) -> bool: ...
    def recover_from_compaction(self) -> str: ...
    def session_status(self) -> dict: ...
```

### SessionWriter

`qanot.session.SessionWriter`

```python
class SessionWriter:
    def __init__(self, sessions_dir: str = "/data/sessions"): ...

    def log_user_message(self, text: str, parent_id: str = "") -> str: ...
    def log_assistant_message(
        self, text: str, tool_uses: list[dict] | None = None,
        usage: Usage | None = None, parent_id: str = "", model: str = "",
    ) -> str: ...
    def new_session(self, session_id: str | None = None) -> None: ...
```

### CronScheduler

`qanot.scheduler.CronScheduler`

```python
class CronScheduler:
    def __init__(
        self, config: Config, provider: LLMProvider,
        tool_registry: ToolRegistry, main_agent: Agent | None = None,
        message_queue: asyncio.Queue | None = None,
    ): ...

    def start(self) -> None: ...
    async def reload_jobs(self) -> None: ...
    def stop(self) -> None: ...
```

### TelegramAdapter

`qanot.telegram.TelegramAdapter`

```python
class TelegramAdapter:
    def __init__(
        self, config: Config, agent: Agent,
        scheduler: CronScheduler | None = None,
    ): ...

    async def start(self) -> None: ...
```

## Memory Functions

`qanot.memory`

```python
def wal_scan(user_message: str) -> list[WALEntry]: ...
def wal_write(entries: list[WALEntry], workspace_dir: str = "/data/workspace") -> None: ...
def write_daily_note(content: str, workspace_dir: str = "/data/workspace") -> None: ...
def memory_search(query: str, workspace_dir: str = "/data/workspace") -> list[dict]: ...
def add_write_hook(hook: Callable[[str, str], None]) -> None: ...
```

## Text Processing Functions

```python
# qanot.context
def truncate_tool_result(result: str, max_chars: int = 8000) -> str: ...

# qanot.rag.chunker
def chunk_text(text: str, max_tokens: int = 512, overlap: int = 64, separator: str | None = None) -> list[str]: ...
```
