# Sozlash ma'lumotnomasi

Qanot AI bitta `config.json` fayl orqali sozlanadi. Bu sahifada barcha maydonlar tushuntirilgan.

## Config fayl joylashuvi

Config fayl quyidagi tartibda qidiriladi:

1. `qanot start <path>` ga berilgan yo'l
2. `QANOT_CONFIG` environment variable
3. Joriy papkadagi `./config.json`
4. `/data/config.json` (Docker default)

## To'liq ma'lumotnoma

### Asosiy sozlamalar

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `bot_token` | string | `""` | BotFather dan olingan Telegram bot token. Majburiy. |
| `provider` | string | `"anthropic"` | LLM provider: `anthropic`, `openai`, `gemini`, `groq` |
| `model` | string | `"claude-sonnet-4-6"` | Tanlangan provider uchun model nomi |
| `api_key` | string | `""` | Provider uchun API key |
| `owner_name` | string | `""` | Bot egasining ismi (system prompt ga kiritiladi) |
| `bot_name` | string | `""` | Botning ko'rsatiladigan nomi (system prompt ga kiritiladi) |
| `timezone` | string | `"Asia/Tashkent"` | Cron va vaqt belgilari uchun IANA timezone |

### Context va compaction

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `max_context_tokens` | int | `200000` | Maksimal context oynasi hajmi (tokenlarda) |
| `compaction_mode` | string | `"safeguard"` | Compaction strategiyasi (hozircha faqat `safeguard`) |
| `max_concurrent` | int | `4` | Bir vaqtda qayta ishlanadigan xabarlar soni |

Context boshqaruv chegaralari (hardcoded, o'zgartirib bo'lmaydi):

- **50%** -- Working Buffer yoqiladi, suhbatlar `working-buffer.md` ga yoziladi
- **60%** -- Proaktiv compaction ishga tushadi, o'rtadagi xabarlar tarixdan o'chiriladi
- **35%** -- Compaction dan keyin maqsadli context hajmi

### Telegram sozlamalari

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `response_mode` | string | `"stream"` | Javob usuli. Pastga qarang. |
| `stream_flush_interval` | float | `0.8` | Streaming draft yangilanishlari orasidagi soniyalar |
| `telegram_mode` | string | `"polling"` | Transport: `polling` yoki `webhook` |
| `webhook_url` | string | `""` | Webhook rejimi uchun ochiq URL (masalan, `https://bot.example.com`) |
| `webhook_port` | int | `8443` | Webhook HTTP server uchun lokal port |
| `allowed_users` | list[int] | `[]` | Botni ishlata oladigan Telegram user IDlari. Bo'sh = hammaga ruxsat. |

**Javob rejimlari:**

| Rejim | Mexanizm | Xulqi |
|-------|----------|-------|
| `stream` | `sendMessageDraft` (Bot API 9.5) | Real-time belgi oqimi. Yangi Telegram klientlar kerak. |
| `partial` | `editMessageText` | Avval xabar yuboriladi, keyin oraliq yangilanishlar bilan tahrir qilinadi. |
| `blocked` | `sendMessage` | To'liq javob kutiladi, keyin bir marta yuboriladi. Eng oddiy lekin eng sekin. |

### Papka yo'llari

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `workspace_dir` | string | `"/data/workspace"` | Agent workspace (SOUL.md, TOOLS.md, xotira) |
| `sessions_dir` | string | `"/data/sessions"` | JSONL session log papkasi |
| `cron_dir` | string | `"/data/cron"` | Cron vazifa ta'riflari (jobs.json) |
| `plugins_dir` | string | `"/data/plugins"` | Tashqi pluginlar papkasi |

`qanot init` ishlatilganda, bu yo'llar loyiha papkasiga nisbatan o'rnatiladi.

### RAG sozlamalari

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `rag_enabled` | bool | `true` | RAG document indexing va qidiruvni yoqish |
| `rag_mode` | string | `"auto"` | RAG strategiyasi: `auto` (tegishli bo'lsa kiritadi), `agentic` (agent o'zi toollar orqali qaror qiladi), `always` (har navbatda kiritadi) |

RAG embedding uchun Gemini yoki OpenAI provider kerak. Batafsil [RAG hujjatlari](rag.md) ga qarang.

### Ovoz sozlamalari

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `voice_provider` | string | `"muxlisa"` | Voice provider: `muxlisa`, `kotib`, `aisha`, `whisper` |
| `voice_api_key` | string | `""` | Voice provider uchun default API key |
| `voice_api_keys` | dict | `{}` | Har provider uchun alohida API key: `{"muxlisa": "...", "kotib": "..."}` |
| `voice_mode` | string | `"inbound"` | Ovoz rejimi: `off` (o'chirilgan), `inbound` (faqat STT), `always` (STT + TTS) |
| `voice_name` | string | `""` | Ovoz nomi (masalan, Muxlisa uchun `maftuna`/`asomiddin`, KotibAI uchun `aziza`/`sherzod`) |
| `voice_language` | string | `""` | STT tilini majburlash (`uz`/`ru`/`en`). Bo'sh = avto-aniqlash. |

### Web qidiruv

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `brave_api_key` | string | `""` | Brave Search API key (bepul tarif: oyiga 2000 so'rov) |

### UX sozlamalari

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `reactions_enabled` | bool | `false` | Xabarlarga emoji reaksiya yuborish |
| `reply_mode` | string | `"coalesced"` | Javob xulqi: `off`, `coalesced`, `always` |

### Guruh chat

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `group_mode` | string | `"mention"` | Guruh xulqi: `off` (guruhlarni e'tiborsiz qoldirish), `mention` (@bot va javoblarga javob berish), `all` (hamma narsaga javob berish) |

### Heartbeat

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `heartbeat_enabled` | bool | `true` | Heartbeat cron jobni yoqish/o'chirish |
| `heartbeat_interval` | string | `"0 */4 * * *"` | Heartbeat uchun cron ifodasi |

### Kunlik brifing

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `briefing_enabled` | bool | `true` | Kunlik ertalabki brifingni yoqish/o'chirish |
| `briefing_schedule` | string | `"0 8 * * *"` | Brifing uchun cron ifodasi (default: har kuni soat 8:00) |

### Xotira va tarix

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `max_memory_injection_chars` | int | `4000` | RAG/compaction natijalarini user xabarlariga kiritish uchun maks belgilar |
| `history_limit` | int | `50` | Qayta ishga tushirishda session tarixdan tiklanadigan maks user navbatlari |

### Extended thinking

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `thinking_level` | string | `"off"` | Claude reasoning rejimi: `off`, `low`, `medium`, `high` |
| `thinking_budget` | int | `10000` | Maksimal thinking tokenlar |
| `thinking_display` | string | `"omitted"` | Thinking mazmunini foydalanuvchiga ko'rsatish: `"omitted"` (tezroq TTFT), `"full"` (to'liq ko'rsatish) |

### Ijro xavfsizligi

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `exec_security` | string | `"cautious"` | Buyruq bajarish xavfsizlik darajasi: `open` (barcha buyruqlar), `cautious` (xavfli operatsiyalar uchun so'raydi), `strict` (faqat ruxsat berilganlar) |
| `exec_allowlist` | list[string] | `[]` | `strict` rejimda faqat shu buyruqlarga ruxsat beriladi |

### Dashboard

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `dashboard_enabled` | bool | `true` | Web dashboard ni yoqish |
| `dashboard_port` | int | `8765` | Dashboard uchun port |
| `dashboard_host` | string | `"127.0.0.1"` | Dashboard server host (Docker uchun `"0.0.0.0"` qo'ying) |

### Backup

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `backup_enabled` | bool | `true` | Ishga tushirishda avtomatik workspace backup |

### Model routing

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `routing_enabled` | bool | `false` | Narxni optimallashtirish uchun 3 bosqichli model routing ni yoqish |
| `routing_model` | string | `"claude-haiku-4-5-20251001"` | Oddiy xabarlar uchun arzon model (salomlashish, tasdiqlash) |
| `routing_mid_model` | string | `"claude-sonnet-4-6"` | O'rta darajali suhbat uchun model |
| `routing_threshold` | float | `0.3` | Routing qarorlari uchun murakkablik ball chegarasi (0.0--1.0) |

### Rasm yaratish

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `image_api_key` | string | `""` | Rasm yaratish uchun Gemini API key (ixtiyoriy, bo'sh bo'lsa provider key ishlatiladi) |
| `image_model` | string | `"gemini-3-pro-image-preview"` | Rasm yaratish va tahrirlash uchun model |

### Multi-Agent

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `agents` | list[AgentDefinition] | `[]` | Delegatsiya uchun agent ta'riflari. Pastga qarang. |
| `monitor_group_id` | int | `0` | Monitoring uchun agent suhbatlarini ko'rsatadigan Telegram guruh ID |

Har bir agent ta'rifi:

```json
{
  "id": "researcher",
  "name": "Tadqiqotchi",
  "prompt": "You are a research assistant...",
  "model": "",
  "provider": "",
  "api_key": "",
  "bot_token": "",
  "tools_allow": [],
  "tools_deny": [],
  "delegate_allow": [],
  "max_iterations": 15,
  "timeout": 120
}
```

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `id` | string | majburiy | Noyob identifikator (masalan, `researcher`, `coder`) |
| `name` | string | `""` | Ko'rsatiladigan ism |
| `prompt` | string | `""` | System prompt / shaxsiyat |
| `model` | string | `""` | Model override (bo'sh = asosiy model) |
| `provider` | string | `""` | Provider override (bo'sh = asosiy provider) |
| `api_key` | string | `""` | API key override (bo'sh = asosiy) |
| `bot_token` | string | `""` | Alohida Telegram bot token (bo'sh = faqat ichki agent) |
| `tools_allow` | list[string] | `[]` | Tool ruxsatnomasi (bo'sh = barcha toollar) |
| `tools_deny` | list[string] | `[]` | Taqiqlangan toollar |
| `delegate_allow` | list[string] | `[]` | Bu agent qaysi agentlarga delegatsiya qila oladi (bo'sh = hammaga) |
| `max_iterations` | int | `15` | Maks tool-use takrorlari |
| `timeout` | int | `120` | Timeout (soniyalarda) |

### Plugin sozlash

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `plugins` | list | `[]` | Plugin sozlamalari. Pastga qarang. |

Har bir plugin yozuvi:

```json
{
  "name": "myplugin",
  "enabled": true,
  "config": {
    "api_url": "https://example.com",
    "username": "admin"
  }
}
```

| Maydon | Tur | Tavsif |
|--------|-----|--------|
| `name` | string | Plugin papka nomi (`plugins/` ichki, keyin `plugins_dir` da qidiriladi) |
| `enabled` | bool | Bu plugin ni yuklash yoki yo'q |
| `config` | dict | `plugin.setup(config)` ga beriladigan ixtiyoriy config |

### MCP serverlar

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `mcp_servers` | list | `[]` | MCP server ta'riflari. Har biri `name`, `command`, `args`, va ixtiyoriy `env` o'z ichiga oladi. |

Har bir MCP server yozuvi:

```json
{
  "name": "github",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-github"],
  "env": {"GITHUB_TOKEN": "ghp_..."}
}
```

MCP klient o'rnatish uchun: `pip install qanot[mcp]`

### Kod bajarish

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `code_execution` | bool | `false` | Anthropic server tomonida kod bajarish toolini yoqish (`code_execution_20250825`) |

Faqat Anthropic provider bilan ishlaydi. Web search bilan birga bepul.

### Webhook va WebChat

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `webhook_secret` | string | `""` | Tashqi webhook endpoint uchun autentifikatsiya kaliti |
| `webchat_enabled` | bool | `false` | WebSocket asosidagi webchat adapterni yoqish |

Webhook endpoint tashqi voqealar uchun ishlatiladi (GitHub, CRM, CI/CD). WebChat WebSocket streaming bilan web interfeys uchun.

### Ko'p provider sozlash

Bitta provider maydonlari (`provider`, `model`, `api_key`) o'rniga, avtomatik failover uchun bir nechta provider sozlash mumkin:

| Maydon | Tur | Default | Tavsif |
|--------|-----|---------|--------|
| `providers` | list | `[]` | Provider profillari ro'yxati. O'rnatilganda failover rejimi yoqiladi. |

Har bir provider profili:

```json
{
  "name": "claude-main",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-...",
  "base_url": ""
}
```

Failover haqida batafsil [Providerlar](providers.md) ga qarang.

## Config misollari

### Minimal (bitta provider, polling)

```json
{
  "bot_token": "123456:ABC-DEF...",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-..."
}
```

### Failover bilan ko'p provider

```json
{
  "bot_token": "123456:ABC-DEF...",
  "providers": [
    {
      "name": "claude-main",
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "api_key": "sk-ant-..."
    },
    {
      "name": "gemini-backup",
      "provider": "gemini",
      "model": "gemini-2.5-flash",
      "api_key": "AIza..."
    },
    {
      "name": "groq-fast",
      "provider": "groq",
      "model": "llama-3.3-70b-versatile",
      "api_key": "gsk_..."
    }
  ],
  "owner_name": "Sardor",
  "bot_name": "Javis",
  "timezone": "Asia/Tashkent",
  "rag_enabled": true
}
```

### Production webhook bilan

```json
{
  "bot_token": "123456:ABC-DEF...",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-...",
  "telegram_mode": "webhook",
  "webhook_url": "https://bot.example.com",
  "webhook_port": 8443,
  "response_mode": "stream",
  "allowed_users": [123456789, 987654321],
  "max_concurrent": 8
}
```

### Tejamkor variant (Groq, bepul tarif)

```json
{
  "bot_token": "123456:ABC-DEF...",
  "provider": "groq",
  "model": "llama-3.3-70b-versatile",
  "api_key": "gsk_...",
  "response_mode": "partial",
  "rag_enabled": false,
  "max_context_tokens": 32000
}
```

Eslatma: Groq embedding qo'llab-quvvatlamaydi, shuning uchun RAG alohida Gemini yoki OpenAI provider talab qiladi. `rag_enabled: false` bo'lsa, RAG toollar ro'yxatga olinmaydi.

### Lokal dasturlash

`qanot init` ishlatilganda, yo'llar loyiha papkasiga nisbatan o'rnatiladi:

```json
{
  "bot_token": "123456:ABC-DEF...",
  "provider": "openai",
  "model": "gpt-4.1",
  "api_key": "sk-...",
  "workspace_dir": "/home/user/mybot/workspace",
  "sessions_dir": "/home/user/mybot/sessions",
  "cron_dir": "/home/user/mybot/cron",
  "plugins_dir": "/home/user/mybot/plugins"
}
```
