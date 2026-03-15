# LLM Providers

Qanot AI supports four LLM providers out of the box, with automatic failover when multiple providers are configured.

## Supported Providers

### Anthropic (Claude)

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-..."
}
```

**Features:**
- Native streaming via `messages.stream()`
- Prompt caching with `cache_control: ephemeral` on the system prompt
- OAuth token support (tokens starting with `sk-ant-oat` use Bearer auth)
- Cost tracking with per-model pricing

**Available models:**

| Model | Input $/MTok | Output $/MTok | Cache Read | Cache Write |
|-------|-------------|---------------|------------|-------------|
| `claude-sonnet-4-6` | 3.00 | 15.00 | 0.30 | 3.75 |
| `claude-opus-4-20250514` | 15.00 | 75.00 | 1.50 | 18.75 |
| `claude-haiku-4-5-20251001` | 0.80 | 4.00 | 0.08 | 1.00 |

**OAuth tokens:** If your API key starts with `sk-ant-oat`, Qanot automatically switches to Bearer authentication with the `anthropic-beta: oauth-2025-04-20` header.

### OpenAI (GPT)

```json
{
  "provider": "openai",
  "model": "gpt-4.1",
  "api_key": "sk-..."
}
```

**Features:**
- Streaming via chat completions with `stream: true`
- Function calling format (tool definitions auto-converted from Anthropic format)
- Usage tracking with `stream_options: include_usage`

**Available models:**

| Model | Input $/MTok | Output $/MTok |
|-------|-------------|---------------|
| `gpt-4.1` | 2.00 | 8.00 |
| `gpt-4.1-mini` | 0.40 | 1.60 |
| `gpt-4o` | 2.50 | 10.00 |
| `gpt-4o-mini` | 0.15 | 0.60 |

### Google Gemini

```json
{
  "provider": "gemini",
  "model": "gemini-2.5-flash",
  "api_key": "AIza..."
}
```

**Features:**
- Uses OpenAI-compatible API via `generativelanguage.googleapis.com`
- Automatic stripping of unsupported JSON Schema keys (`patternProperties`, `additionalProperties`, `$ref`)
- Synthetic user turn insertion (Gemini requires conversations to start with a user message)
- Free embedding tier for RAG (preferred embedder)

**Available models:**

| Model | Input $/MTok | Output $/MTok |
|-------|-------------|---------------|
| `gemini-3.1-pro-preview` | 2.00 | 12.00 |
| `gemini-3.1-flash-lite` | 0.25 | 1.50 |
| `gemini-3-flash-preview` | 0.15 | 0.60 |
| `gemini-2.5-pro` | 1.25 | 10.00 |
| `gemini-2.5-flash` | 0.15 | 0.60 |
| `gemini-2.0-flash` | 0.10 | 0.40 |

**Custom base URL:** You can override the base URL for Gemini, which is useful for proxies or regional endpoints:

```json
{
  "provider": "gemini",
  "model": "gemini-2.5-flash",
  "api_key": "AIza...",
  "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"
}
```

### Groq

```json
{
  "provider": "groq",
  "model": "llama-3.3-70b-versatile",
  "api_key": "gsk_..."
}
```

**Features:**
- Uses OpenAI-compatible API via `api.groq.com`
- Very fast inference (sub-second responses for smaller models)
- Generous free tier

**Available models:**

| Model | Input $/MTok | Output $/MTok |
|-------|-------------|---------------|
| `meta-llama/llama-4-scout-17b-16e-instruct` | 0.11 | 0.18 |
| `llama-3.3-70b-versatile` | 0.59 | 0.79 |
| `llama-3.1-8b-instant` | 0.05 | 0.08 |
| `qwen/qwen3-32b` | 0.29 | 0.39 |
| `moonshotai/kimi-k2-instruct` | 0.20 | 0.20 |
| `groq/compound` | 0.59 | 0.79 |
| `groq/compound-mini` | 0.05 | 0.08 |

**Limitation:** Groq does not offer an embedding API. If Groq is your only provider, RAG will not function unless you also add a Gemini or OpenAI provider.

## Message Format Conversion

Qanot uses Anthropic's message format internally (tool_use/tool_result blocks). The OpenAI, Gemini, and Groq providers automatically convert:

- **Tool definitions:** Anthropic `input_schema` format is converted to OpenAI `function.parameters`
- **Messages:** `tool_use` blocks become `function` tool calls; `tool_result` blocks become `tool` role messages
- **System prompt:** Moved from a dedicated field to a system role message

This conversion is transparent. You do not need to worry about format differences.

## Multi-Provider Failover

When you configure multiple providers, Qanot creates a `FailoverProvider` that automatically switches between them on errors.

### Configuration

```json
{
  "providers": [
    {
      "name": "claude-primary",
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "api_key": "sk-ant-..."
    },
    {
      "name": "gemini-secondary",
      "provider": "gemini",
      "model": "gemini-2.5-flash",
      "api_key": "AIza..."
    },
    {
      "name": "groq-fallback",
      "provider": "groq",
      "model": "llama-3.3-70b-versatile",
      "api_key": "gsk_..."
    }
  ]
}
```

### How Failover Works

1. The first provider in the list is the **active provider**
2. On each API call, Qanot tries the active provider first
3. If it fails with a classified error, the next available provider is tried
4. Successful calls reset the failure state for that provider
5. Failed providers enter a cooldown period

### Error Classification

Errors are classified into categories that determine retry behavior:

| Error Type | HTTP Codes | Behavior |
|------------|-----------|----------|
| `rate_limit` | 429 | Transient -- try next provider, cooldown |
| `overloaded` | 503, 529 | Transient -- try next provider, cooldown |
| `timeout` | 408, 500, 502, 504 | Transient -- try next provider, cooldown |
| `not_found` | 404 | Transient -- try next provider |
| `auth` | 401, 403 | Permanent -- provider disabled until restart |
| `billing` | 402 | Permanent -- provider disabled until restart |
| `unknown` | Other | Not retried, error raised |

### Cooldown Mechanism

- **Transient failures:** Provider enters cooldown for `120 * failure_count` seconds (max 600s)
- **Permanent failures:** Provider is disabled for the session lifetime
- **Success:** Resets failure count and cooldown for that provider

### Provider Initialization

Providers are lazily initialized. The second and third providers are only created when first needed (on failover), reducing startup time and memory usage.

## Adding Custom Providers

Any provider that speaks the OpenAI chat completions API can be used through the `openai` provider type with a custom `base_url`:

```json
{
  "provider": "openai",
  "model": "your-model-name",
  "api_key": "your-key",
  "base_url": "https://your-api.example.com/v1"
}
```

This works for:
- OpenRouter
- Azure OpenAI
- Local models (vLLM, llama.cpp server)
- Any OpenAI-compatible API

### Ollama Native API

For Ollama, Qanot uses the native `/api/chat` endpoint with `think=false` instead of the OpenAI-compatible endpoint. This provides approximately 30x faster inference by disabling the thinking/reasoning step that Ollama's OpenAI compatibility layer does not support efficiently. The native API is automatically selected when Ollama is detected (by API key or base URL containing port 11434).

### FastEmbed for RAG with Ollama

When Ollama is your LLM provider, Qanot automatically selects FastEmbed (CPU-based, ONNX runtime) for RAG embeddings instead of requiring a separate embedding API. This avoids GPU VRAM conflicts between the chat model and the embedding model. Install with `pip install fastembed`. If FastEmbed is not installed, Qanot falls back to using Ollama's own embedding endpoint via the OpenAI-compatible API.

For providers with significant API differences, you can subclass `LLMProvider`:

```python
from qanot.providers.base import LLMProvider, ProviderResponse, StreamEvent

class MyProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        self.model = model
        # Initialize your client

    async def chat(self, messages, tools=None, system=None) -> ProviderResponse:
        # Implement chat
        return ProviderResponse(content="Hello", stop_reason="end_turn")

    async def chat_stream(self, messages, tools=None, system=None):
        # Optional: implement streaming
        # Default falls back to chat() if not overridden
        yield StreamEvent(type="text_delta", text="Hello")
        yield StreamEvent(type="done", response=ProviderResponse(content="Hello"))
```

Register it by modifying `_create_single_provider` in `qanot/providers/failover.py`.
