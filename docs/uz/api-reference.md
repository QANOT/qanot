# API ma'lumotnomasi

Bu sahifa Qanot AI dagi ommaviy class'lar va metodlarni hujjatlashtiradi. Framework'ni kengaytirish yoki maxsus integratsiyalar qurish uchun shu interfeys'lar bilan ishlaysiz.

## Asosiy class'lar

### Agent

`qanot.agent.Agent`

Tool_use loop'ini ishga tushiradigan asosiy agent. Har bir foydalanuvchi suhbatlari, tool bajarish va kontekst kuzatuvini boshqaradi.

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

| Parametr | Tur | Tavsif |
|----------|-----|--------|
| `config` | `Config` | Konfiguratsiya obyekti |
| `provider` | `LLMProvider` | LLM provider instansiyasi |
| `tool_registry` | `ToolRegistry` | Mavjud tool'lar registry'si |
| `session` | `SessionWriter` | Sessiya logeri (None bo'lsa config'dan yaratiladi) |
| `context` | `ContextTracker` | Token kuzatuvchisi (None bo'lsa config'dan yaratiladi) |
| `prompt_mode` | `str` | `"full"`, `"minimal"` yoki `"none"` |

**Metodlar:**

```python
async def run_turn(self, user_message: str, user_id: str | None = None) -> str
```

User xabarini agent loop orqali qayta ishlaydi. Oxirgi matn javobini qaytaradi. Bir xil foydalanuvchi uchun bir vaqtda qayta ishlashni oldini olish uchun har bir foydalanuvchiga lock oladi.

```python
async def run_turn_stream(
    self, user_message: str, user_id: str | None = None
) -> AsyncIterator[StreamEvent]
```

User xabarini streaming bilan qayta ishlaydi. `StreamEvent` obyektlarini kelgancha yield qiladi. Tool-use iteratsiyalari ichki boshqariladi; har bir iteratsiyadagi matn delta'lari yield qilinadi.

```python
def reset(self, user_id: str | None = None) -> None
```

Suhbat holatini tiklaydi. `user_id` berilsa, faqat o'sha foydalanuvchini tiklaydi. `None` bo'lsa, barcha foydalanuvchilarni tiklaydi.

**Konstantalar:**

| Konstanta | Qiymat | Tavsif |
|-----------|--------|--------|
| `MAX_ITERATIONS` | 25 | Har bir navbat uchun maksimal tool_use loop iteratsiyalari |
| `MAX_SAME_ACTION` | 3 | N ta bir xil ketma-ket tool chaqiruvdan keyin to'xtatish |
| `TOOL_TIMEOUT` | 30 | Har bir tool bajarilishi uchun soniyalar |
| `CONVERSATION_TTL` | 3600 | Bo'sh turgan suhbatlar o'chirilishidan oldingi soniyalar |

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

Bitta prompt uchun yangi agent yaratadi va ishga tushiradi. Cron job'lar tomonidan ishlatiladi. Oxirgi javob matnini qaytaradi. Kichikroq tizim prompt'lari uchun `prompt_mode="minimal"` ishlatadi.

### ToolRegistry

`qanot.agent.ToolRegistry`

Mavjud tool'larning registry'si.

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

| Metod | Tavsif |
|-------|--------|
| `register()` | Tool'ni handler funksiyasi bilan ro'yxatga olish |
| `get_definitions()` | LLM-mos formatda tool ta'riflarini olish |
| `execute()` | Tool'ni nomi bo'yicha timeout himoyasi bilan bajarish |
| `tool_names` | Ro'yxatga olingan tool nomlari ro'yxati |

### Config

`qanot.config.Config`

```python
@dataclass
class Config:
    bot_token: str = ""
    # Legacy bitta provider maydonlari (hali qo'llab-quvvatlanadi)
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""
    # Ko'p provayderli qo'llab-quvvatlash
    providers: list[ProviderConfig] = field(default_factory=list)
    # Yo'llar
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
    response_mode: str = "stream"          # "stream" | "partial" | "blocked"
    stream_flush_interval: float = 0.8     # draft yangilanishlari orasidagi soniyalar
    telegram_mode: str = "polling"         # "polling" | "webhook"
    webhook_url: str = ""                  # masalan "https://bot.example.com/webhook"
    webhook_port: int = 8443               # webhook server uchun lokal port
    # RAG
    rag_enabled: bool = True
    rag_mode: str = "auto"                 # "auto" | "agentic" | "always"
    # Ovoz
    voice_provider: str = "muxlisa"        # "muxlisa" | "kotib" | "aisha" | "whisper"
    voice_api_key: str = ""                # Standart API kalit (fallback)
    voice_api_keys: dict[str, str] = field(default_factory=dict)  # Provider bo'yicha kalitlar
    voice_mode: str = "inbound"            # "off" | "inbound" | "always"
    voice_name: str = ""                   # TTS ovoz nomi
    voice_language: str = ""               # STT tilini majburlash (uz/ru/en), bo'sh bo'lsa auto
    # Web search
    brave_api_key: str = ""                # Brave Search API kaliti
    # UX
    reactions_enabled: bool = False        # Xabarlarga emoji reaktsiyalar yuborish
    reply_mode: str = "coalesced"          # "off" | "coalesced" | "always"
    # Guruh chat
    group_mode: str = "mention"            # "off" | "mention" | "all"
    # O'z-o'zini tiklash / heartbeat
    heartbeat_enabled: bool = True
    heartbeat_interval: str = "0 */4 * * *"
    # Kunlik brifing
    briefing_enabled: bool = True
    briefing_schedule: str = "0 8 * * *"
    # Xotira inject byudjeti
    max_memory_injection_chars: int = 4000
    # Sessiya tarix qayta ishlash
    history_limit: int = 50
    # Kengaytirilgan fikrlash (Claude reasoning rejimi)
    thinking_level: str = "off"            # "off" | "low" | "medium" | "high"
    thinking_budget: int = 10000           # maksimal fikrlash tokenlari
    # Bajarish xavfsizligi
    exec_security: str = "open"            # "open" | "cautious" | "strict"
    exec_allowlist: list[str] = field(default_factory=list)
    # Dashboard
    dashboard_enabled: bool = True
    dashboard_port: int = 8765
    # Backup
    backup_enabled: bool = True
    # Model routing (narx optimallashtirish)
    routing_enabled: bool = False
    routing_model: str = "claude-haiku-4-5-20251001"
    routing_mid_model: str = "claude-sonnet-4-6"
    routing_threshold: float = 0.3         # Murakkablik balli chegarasi (0.0-1.0)
    # Rasm generatsiyasi
    image_api_key: str = ""                # Rasmlar uchun alohida Gemini kalit
    image_model: str = "gemini-3-pro-image-preview"
    # Ko'p agentli ta'riflar
    agents: list[AgentDefinition] = field(default_factory=list)
    # Agent monitoring
    monitor_group_id: int = 0              # Monitoring uchun Telegram guruh ID
```

```python
def load_config(path: str | None = None) -> Config
```

JSON fayldan konfiguratsiyani yuklaydi. `path` None bo'lsa, `QANOT_CONFIG` env var'ni tekshiradi, keyin `/data/config.json` ga qaytadi.

### ProviderConfig

`qanot.config.ProviderConfig`

```python
@dataclass
class ProviderConfig:
    name: str
    provider: str       # "anthropic" | "openai" | "gemini" | "groq"
    model: str
    api_key: str
    base_url: str = ""
```

### PluginConfig

`qanot.config.PluginConfig`

```python
@dataclass
class PluginConfig:
    name: str
    enabled: bool = True
    config: dict = field(default_factory=dict)
```

### AgentDefinition

`qanot.config.AgentDefinition`

```python
@dataclass
class AgentDefinition:
    id: str                                              # Noyob identifikator
    name: str = ""                                       # Inson o'qiy oladigan nom
    prompt: str = ""                                     # Tizim prompt / shaxsiyat
    model: str = ""                                      # Model almashtirish (bo'sh = asosiyni ishlatish)
    provider: str = ""                                   # Provider almashtirish (bo'sh = asosiyni ishlatish)
    api_key: str = ""                                    # API kalit almashtirish (bo'sh = asosiyni ishlatish)
    bot_token: str = ""                                  # Alohida Telegram bot token (bo'sh = ichki agent)
    tools_allow: list[str] = field(default_factory=list) # Oq ro'yxat (bo'sh = barchasi ruxsat)
    tools_deny: list[str] = field(default_factory=list)  # Qora ro'yxat
    delegate_allow: list[str] = field(default_factory=list)  # Delegatsiya maqsadlari (bo'sh = barchasi)
    max_iterations: int = 15                             # Maksimal tool-use loop'lar
    timeout: int = 120                                   # Timeout oldidan soniyalar
```

## Provider class'lari

### LLMProvider

`qanot.providers.base.LLMProvider`

LLM provider'lar uchun abstrakt bazaviy class.

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

`chat_stream()` ning standart implementatsiyasi bor — `chat()` ga qaytadi. Provider'lar haqiqiy streaming uchun buni override qilishi mumkin.

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
    response: ProviderResponse | None = None  # "done" da o'rnatiladi
```

### ToolCall

`qanot.providers.base.ToolCall`

```python
@dataclass
class ToolCall:
    id: str        # Provider tomonidan tayinlangan ID
    name: str      # Tool nomi
    input: dict    # Tool parametrlari
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

### Konkret provider'lar

| Class | Modul | Provider |
|-------|-------|----------|
| `AnthropicProvider` | `qanot.providers.anthropic` | Anthropic Claude |
| `OpenAIProvider` | `qanot.providers.openai` | OpenAI GPT |
| `GeminiProvider` | `qanot.providers.gemini` | Google Gemini |
| `GroqProvider` | `qanot.providers.groq` | Groq |
| `FailoverProvider` | `qanot.providers.failover` | Ko'p provayderli failover wrapper |

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

### Xato klassifikatsiyasi

`qanot.providers.errors`

```python
def classify_error(error: Exception) -> str
```

Quyidagilardan birini qaytaradi: `rate_limit`, `auth`, `billing`, `overloaded`, `timeout`, `not_found`, `unknown`.

```python
PERMANENT_FAILURES = {"auth", "billing"}
TRANSIENT_FAILURES = {"rate_limit", "overloaded", "timeout", "not_found"}
```

## RAG class'lari

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

    # Async wrapper'lar (VectorStore'dan meros)
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
    score: float  # 0..1, yuqori yaxshiroq
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

Konkret implementatsiyalar: `GeminiEmbedder` (768 o'lcham), `OpenAIEmbedder` (1536 o'lcham).

```python
def create_embedder(config) -> Embedder | None
```

Config'dan eng yaxshi mavjud embedder'ni avtomatik aniqlaydi. Mos provider topilmasa None qaytaradi.

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

## Plugin class'lari

### Plugin

`qanot.plugins.base.Plugin`

```python
class Plugin(ABC):
    name: str = ""
    description: str = ""
    tools_md: str = ""       # Workspace TOOLS.md ga qo'shiladi
    soul_append: str = ""    # Workspace SOUL.md ga qo'shiladi

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

### tool dekoratori

`qanot.plugins.base.tool`

```python
def tool(name: str, description: str, parameters: dict | None = None)
```

Metodni tool sifatida belgilaydigan dekorator. Dekoratsiya qilingan metod async bo'lishi, `self` va `params: dict` qabul qilishi va `str` qaytarishi kerak.

## Yordamchi class'lar

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

`session_status()` qaytaradi:

| Kalit | Tur | Tavsif |
|-------|-----|--------|
| `context_percent` | `float` | Joriy kontekst ishlatilishi foiz sifatida (1 kasrgacha yaxlitlangan) |
| `context_tokens` | `int` | Oxirgi prompt tokenlari (haqiqiy kontekst oynasi ishlatilishi) |
| `total_output_tokens` | `int` | Jami yig'ilgan chiqish tokenlari |
| `total_tokens` | `int` | `context_tokens` + `total_output_tokens` |
| `max_tokens` | `int` | Maksimal kontekst oynasi hajmi |
| `buffer_active` | `bool` | Working buffer faol yoki yo'q (50% chegarasi o'tilgan) |
| `buffer_started` | `str \| None` | Buffer faollashgan vaqtning ISO belgisi |
| `turn_count` | `int` | Sessiyada user navbatlari soni |
| `api_calls` | `int` | Jami API chaqiruvlari (tool loop iteratsiyalari bilan birga) |

### CostTracker

`qanot.context.CostTracker`

Har bir foydalanuvchi uchun token va narx kuzatuvi. Workspace papkasidagi `costs.json` ga saqlanadi.

```python
class CostTracker:
    def __init__(self, workspace_dir: str = "/data/workspace"): ...

    def add_usage(
        self, user_id: str, input_tokens: int = 0, output_tokens: int = 0,
        cache_read: int = 0, cache_write: int = 0, cost: float = 0.0,
    ) -> None: ...
    def add_turn(self, user_id: str) -> None: ...
    def get_user_stats(self, user_id: str) -> dict: ...
    def get_all_stats(self) -> dict[str, dict]: ...
    def get_total_cost(self) -> float: ...
    def save(self) -> None: ...
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

## Xotira funksiyalari

`qanot.memory`

```python
def wal_scan(user_message: str) -> list[WALEntry]: ...
def wal_write(entries: list[WALEntry], workspace_dir: str = "/data/workspace") -> None: ...
def write_daily_note(content: str, workspace_dir: str = "/data/workspace") -> None: ...
def memory_search(query: str, workspace_dir: str = "/data/workspace") -> list[dict]: ...
def add_write_hook(hook: Callable[[str, str], None]) -> None: ...
```

## Matn qayta ishlash funksiyalari

```python
# qanot.context
def truncate_tool_result(result: str, max_chars: int = 8000) -> str: ...

# qanot.rag.chunker
def chunk_text(text: str, max_tokens: int = 512, overlap: int = 64, separator: str | None = None) -> list[str]: ...
```
