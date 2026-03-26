# Boshlash

Ikki buyruq — bot ishlaydi.

```bash
pip install qanot
qanot init
```

`qanot init` — interactive wizard. Bot token, provider, model, API key so'raydi. Hech qanday faylni qo'lda tahrirlash kerak emas.

## Oldindan kerak bo'ladigan narsalar

- Python 3.11 yoki undan yuqori
- Telegram bot token — [@BotFather](https://t.me/BotFather) dan oling
- Kamida bitta LLM provider uchun API key (Anthropic, OpenAI, Google Gemini, yoki Groq)

## O'rnatish

PyPI dan o'rnating:

```bash
pip install qanot
```

RAG (document indexing va semantik qidiruv) kerak bo'lsa, qo'shimcha dependency bilan o'rnating:

```bash
pip install qanot[rag]
```

Bu `sqlite-vec` ni o'rnatadi -- vector saqlash uchun SQLite extension.

MCP klient (tashqi tool serverlarga ulanish) kerak bo'lsa:

```bash
pip install qanot[mcp]
```

Brauzer toollar (sahifalarni ko'rish, bosish, forma to'ldirish, skrinshot) kerak bo'lsa:

```bash
pip install qanot[browser]
```

Bir nechta extralarni birlashtirish mumkin:

```bash
pip install qanot[rag,mcp,browser]
```

## Loyiha yaratish

CLI orqali yangi loyiha yarating:

```bash
qanot init mybot
```

Bu `mybot/` papkasini `config.json` fayl bilan yaratadi. Birinchi ishga tushirishdan keyin struktura shunday ko'rinadi:

```
mybot/
├── config.json          # Sizning config
├── workspace/           # Agent workspace (birinchi ishga tushirishda yaratiladi)
│   ├── SOUL.md          # Agent shaxsiyati va ko'rsatmalar
│   ├── TOOLS.md         # Agent uchun tool hujjatlari
│   ├── IDENTITY.md      # Agent ismi va uslubi
│   ├── SKILL.md         # Proaktiv xatti-harakatlar
│   ├── AGENTS.md        # Ishlash qoidalari
│   ├── MEMORY.md        # Uzoq muddatli xotira
│   ├── SESSION-STATE.md # Joriy session holati (WAL yozuvlari)
│   └── memory/          # Kunlik qaydlar papkasi
├── sessions/            # JSONL session loglar
├── cron/                # Cron vazifa ta'riflari
└── plugins/             # Maxsus plugin papkasi
```

## Sozlash

`qanot init` wizard hammasini so'raydi — qo'lda tahrirlash shart emas. Lekin keyin o'zgartirmoqchi bo'lsangiz, `config.json` shunday ko'rinadi:

```json
{
  "bot_token": "123456:ABC-DEF...",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-...",
  "owner_name": "Sardor",
  "bot_name": "MyAssistant",
  "timezone": "Asia/Tashkent"
}
```

Minimal kerakli maydonlar:

| Maydon | Tavsif |
|--------|--------|
| `bot_token` | BotFather dan olingan Telegram bot token |
| `provider` | LLM provider: `anthropic`, `openai`, `gemini`, yoki `groq` |
| `model` | Model nomi (masalan, `claude-sonnet-4-6`, `gpt-4.1`, `gemini-2.5-flash`) |
| `api_key` | Tanlagan provider uchun API key |

Barcha maydonlar uchun [Sozlash ma'lumotnomasi](configuration.md) ga qarang.

## Botni ishga tushirish

Botni yoqing:

```bash
qanot start mybot
```

Shunday chiqish ko'rinadi:

```
  ___                    _
 / _ \  __ _ _ __   ___ | |_
| | | |/ _` | '_ \ / _ \| __|
| |_| | (_| | | | | (_) | |_
 \__\_\\__,_|_| |_|\___/ \__|

Config: mybot/config.json

2025-01-15 10:00:00 [qanot] INFO: Config loaded: provider=anthropic, model=claude-sonnet-4-6
2025-01-15 10:00:00 [qanot] INFO: Provider initialized: anthropic
2025-01-15 10:00:00 [qanot] INFO: Tools registered: read_file, write_file, list_files, run_command, memory_search, session_status, cost_status, send_file, doctor, cron_create, cron_list, cron_delete, cron_update, web_search, web_fetch, generate_image, edit_image, rag_search, rag_index, rag_list, rag_forget
2025-01-15 10:00:00 [qanot] INFO: Cron scheduler started with 1 jobs
2025-01-15 10:00:01 [qanot.telegram] INFO: [telegram] starting — transport=polling, response=stream, flush=0.8s
```

Telegram ni oching, botni toping va xabar yuboring. Bot streaming rejimda javob beradi.

## Boshqa ishga tushirish usullari

**Environment variable orqali:**

```bash
export QANOT_CONFIG=/path/to/config.json
qanot start
```

**Python modul sifatida:**

```bash
QANOT_CONFIG=mybot/config.json python3 -m qanot
```

**Docker (production uchun):**

```dockerfile
FROM python:3.11-slim
RUN pip install qanot[rag]
COPY config.json /data/config.json
CMD ["qanot", "start"]
```

Docker da default yo'llar (`/data/workspace`, `/data/sessions` va h.k.) o'zgartirmasdan ishlaydi.

## Birinchi suhbat

Telegram da botga xabar yuboring. Ichkarida nima bo'ladi:

1. Telegram adapter xabarni qabul qiladi
2. WAL protocol tuzatishlar, afzalliklar va qarorlarni skanerlaydi
3. Agent workspace fayllardan system prompt tuzadi
4. LLM javob yaratadi, kerak bo'lsa toollarni ishlatadi
5. Javob real-time da Telegram ga stream qilinadi
6. Suhbat kunlik qaydlar va session fayllariga yoziladi

Bot o'z workspace da fayllarni o'qiy va yoza oladi, web qidiradi, sandboxed buyruqlarni bajaradi va cron joblarni boshqaradi -- hammasi tabiiy suhbat orqali.

## Botni sozlash

Workspace fayllarni tahrirlang -- bot xatti-harakatini shakllantiradi:

- **`workspace/SOUL.md`** -- Asosiy shaxsiyat, ko'rsatmalar va xulq qoidalari
- **`workspace/IDENTITY.md`** -- Ism, muloqot uslubi, emoji afzalliklari
- **`workspace/SKILL.md`** -- Proaktiv xatti-harakatlar va o'z-o'zini yaxshilash
- **`workspace/TOOLS.md`** -- Agent uchun tool hujjatlari

Bu fayllar har navbatda system prompt ga kiritiladi. O'zgarishlar darhol kuchga kiradi.

## Keyingi qadamlar

- [Sozlash ma'lumotnomasi](configuration.md) -- to'liq config maydonlar ma'lumotnomasi
- [LLM Providerlar](providers.md) -- bir nechta provider sozlash, failover
- [Toollar](tools.md) -- tayyor toollar va maxsus toollar yaratish
- [Xotira tizimi](memory.md) -- bot suhbatlar orasida qanday eslab qoladi
