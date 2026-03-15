# LLM Providerlar

Qanot AI to'rtta LLM providerni qo'llab-quvvatlaydi, bir nechta provider sozlangan bo'lsa avtomatik failover ishlaydi.

## Qo'llab-quvvatlanadigan providerlar

### Anthropic (Claude)

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-..."
}
```

**Imkoniyatlari:**
- `messages.stream()` orqali native streaming
- System promptda `cache_control: ephemeral` bilan prompt caching
- OAuth token qo'llab-quvvatlashi (`sk-ant-oat` bilan boshlanadigan tokenlar Bearer auth ishlatadi)
- Har bir model narxi bo'yicha xarajat kuzatish

**Mavjud modellar:**

| Model | Kirish $/MTok | Chiqish $/MTok | Cache o'qish | Cache yozish |
|-------|--------------|----------------|-------------|-------------|
| `claude-sonnet-4-6` | 3.00 | 15.00 | 0.30 | 3.75 |
| `claude-opus-4-20250514` | 15.00 | 75.00 | 1.50 | 18.75 |
| `claude-haiku-4-5-20251001` | 0.80 | 4.00 | 0.08 | 1.00 |

**OAuth tokenlar:** API kalit `sk-ant-oat` bilan boshlansa, Qanot avtomatik `anthropic-beta: oauth-2025-04-20` headeri bilan Bearer autentifikatsiyaga o'tadi.

### OpenAI (GPT)

```json
{
  "provider": "openai",
  "model": "gpt-4.1",
  "api_key": "sk-..."
}
```

**Imkoniyatlari:**
- `stream: true` bilan chat completions orqali streaming
- Function calling formati (tool definitionlar Anthropic formatidan avtomatik konvertatsiya qilinadi)
- `stream_options: include_usage` bilan foydalanish kuzatish

**Mavjud modellar:**

| Model | Kirish $/MTok | Chiqish $/MTok |
|-------|--------------|----------------|
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

**Imkoniyatlari:**
- `generativelanguage.googleapis.com` orqali OpenAI-mos API ishlatadi
- Qo'llab-quvvatlanmaydigan JSON Schema kalitlarni avtomatik olib tashlaydi (`patternProperties`, `additionalProperties`, `$ref`)
- Sintetik user turn qo'shish (Gemini suhbat user xabari bilan boshlanishini talab qiladi)
- RAG uchun bepul embedding darajasi (afzal embedder)

**Mavjud modellar:**

| Model | Kirish $/MTok | Chiqish $/MTok |
|-------|--------------|----------------|
| `gemini-3.1-pro-preview` | 2.00 | 12.00 |
| `gemini-3.1-flash-lite` | 0.25 | 1.50 |
| `gemini-3-flash-preview` | 0.15 | 0.60 |
| `gemini-2.5-pro` | 1.25 | 10.00 |
| `gemini-2.5-flash` | 0.15 | 0.60 |
| `gemini-2.0-flash` | 0.10 | 0.40 |

**Maxsus base URL:** Gemini uchun base URL ni o'zgartirish mumkin, proksi yoki mintaqaviy endpointlar uchun foydali:

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

**Imkoniyatlari:**
- `api.groq.com` orqali OpenAI-mos API ishlatadi
- Juda tez inference (kichik modellar uchun bir soniyadan kam)
- Keng bepul daraja

**Mavjud modellar:**

| Model | Kirish $/MTok | Chiqish $/MTok |
|-------|--------------|----------------|
| `meta-llama/llama-4-scout-17b-16e-instruct` | 0.11 | 0.18 |
| `llama-3.3-70b-versatile` | 0.59 | 0.79 |
| `llama-3.1-8b-instant` | 0.05 | 0.08 |
| `qwen/qwen3-32b` | 0.29 | 0.39 |
| `moonshotai/kimi-k2-instruct` | 0.20 | 0.20 |
| `groq/compound` | 0.59 | 0.79 |
| `groq/compound-mini` | 0.05 | 0.08 |

**Cheklov:** Groq embedding API taqdim etmaydi. Groq yagona provideringiz bo'lsa, Gemini yoki OpenAI providerni ham qo'shmasangiz RAG ishlamaydi.

## Xabar formati konvertatsiyasi

Qanot ichki sifatida Anthropic xabar formatini ishlatadi (tool_use/tool_result bloklari). OpenAI, Gemini va Groq providerlari avtomatik konvertatsiya qiladi:

- **Tool definitionlar:** Anthropic `input_schema` formati OpenAI `function.parameters` ga aylantiriladi
- **Xabarlar:** `tool_use` bloklari `function` tool chaqiruvlariga; `tool_result` bloklari `tool` roli xabarlariga aylanadi
- **System prompt:** Alohida maydondan system roli xabariga ko'chiriladi

Bu konvertatsiya shaffof. Format farqlari haqida tashvishlanishingiz shart emas.

## Ko'p providerli failover

Bir nechta provider sozlasangiz, Qanot xatolarda avtomatik providerlar orasida almashuvchi `FailoverProvider` yaratadi.

### Sozlash

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

### Failover qanday ishlaydi

1. Ro'yxatdagi birinchi provider **aktiv provider** hisoblanadi
2. Har bir API chaqiruvda Qanot avval aktiv providerni sinaydi
3. Tasniflangan xato bo'lsa, navbatdagi mavjud provider sinab ko'riladi
4. Muvaffaqiyatli chaqiruv o'sha providerning xato holatini qayta o'rnatadi
5. Muvaffaqiyatsiz providerlar kutish davriga kiradi

### Xato tasnifi

Xatolar qayta urinish xulq-atvorini belgilovchi kategoriyalarga bo'linadi:

| Xato turi | HTTP kodlar | Xulq-atvor |
|-----------|------------|------------|
| `rate_limit` | 429 | Vaqtinchalik -- keyingi provider, kutish |
| `overloaded` | 503, 529 | Vaqtinchalik -- keyingi provider, kutish |
| `timeout` | 408, 500, 502, 504 | Vaqtinchalik -- keyingi provider, kutish |
| `not_found` | 404 | Vaqtinchalik -- keyingi provider |
| `auth` | 401, 403 | Doimiy -- provider qayta ishga tushirilguncha o'chiriladi |
| `billing` | 402 | Doimiy -- provider qayta ishga tushirilguncha o'chiriladi |
| `unknown` | Boshqa | Qayta urinilmaydi, xato ko'tariladi |

### Kutish mexanizmi

- **Vaqtinchalik xatolar:** Provider `120 * xato_soni` soniya kutishga kiradi (maks 600s)
- **Doimiy xatolar:** Provider sessiya davomida o'chiriladi
- **Muvaffaqiyat:** O'sha provider uchun xato soni va kutish vaqtini qayta o'rnatadi

### Provider initsializatsiyasi

Providerlar lazy initsializatsiya qilinadi. Ikkinchi va uchinchi providerlar faqat birinchi kerak bo'lganda (failoverda) yaratiladi -- bu ishga tushirish vaqti va xotira sarfini kamaytiradi.

## Maxsus providerlar qo'shish

OpenAI chat completions API bilan gaplasha oladigan har qanday provider `openai` provider turi orqali maxsus `base_url` bilan ishlatilishi mumkin:

```json
{
  "provider": "openai",
  "model": "your-model-name",
  "api_key": "your-key",
  "base_url": "https://your-api.example.com/v1"
}
```

Bu quyidagilar uchun ishlaydi:
- OpenRouter
- Azure OpenAI
- Lokal modellar (vLLM, llama.cpp server)
- Har qanday OpenAI-mos API

### Ollama native API

Ollama uchun Qanot OpenAI-mos endpoint o'rniga native `/api/chat` endpointini `think=false` bilan ishlatadi. Bu Ollama ning OpenAI mos qatlami samarali qo'llab-quvvatlamaydigan thinking/reasoning bosqichini o'chirib, taxminan 30x tezroq inference beradi. Native API Ollama aniqlanganda (API kalit yoki base URL da 11434 port bo'lganda) avtomatik tanlanadi.

### Ollama bilan RAG uchun FastEmbed

Ollama LLM provideringiz bo'lganda, Qanot RAG embeddinglar uchun alohida embedding API talab qilish o'rniga avtomatik FastEmbed (CPU-asosli, ONNX runtime) ni tanlaydi. Bu chat modeli va embedding modeli orasidagi GPU VRAM to'qnashuvlarini oldini oladi. `pip install fastembed` bilan o'rnating. FastEmbed o'rnatilmagan bo'lsa, Qanot OpenAI-mos API orqali Ollama ning o'z embedding endpointiga qaytadi.

API farqlari katta bo'lgan providerlar uchun `LLMProvider` dan voris olishingiz mumkin:

```python
from qanot.providers.base import LLMProvider, ProviderResponse, StreamEvent

class MyProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        self.model = model
        # Klientni ishga tushiring

    async def chat(self, messages, tools=None, system=None) -> ProviderResponse:
        # Chat ni qiling
        return ProviderResponse(content="Hello", stop_reason="end_turn")

    async def chat_stream(self, messages, tools=None, system=None):
        # Ixtiyoriy: streaming ni qiling
        # Standart: qayta yozilmasa chat() ga qaytadi
        yield StreamEvent(type="text_delta", text="Hello")
        yield StreamEvent(type="done", response=ProviderResponse(content="Hello"))
```

Uni `qanot/providers/failover.py` dagi `_create_single_provider` ni o'zgartirish orqali ro'yxatdan o'tkazing.
