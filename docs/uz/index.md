# Qanot AI

Telegram bot yaratadigan yengil Python framework.

**PyPI:** `qanot` | **Python:** 3.11+ | **Litsenziya:** MIT

## Qanot AI nima?

Qanot AI -- LLM ni doimiy ishlaydigan, tool ishlatadigan Telegram assistentga aylantiradi. Siz config fayl va Telegram bot token berasiz, Qanot qolgan hammani o'zi qiladi: agent loop, xotira, streaming, cron, va provider failover.

O'zbekiston bozori uchun qurilgan: `Asia/Tashkent` timezone, Telegram-first dizayn (O'zbekistonda Telegram asosiy messenger), va o'zbekcha xato xabarlar.

## Asosiy imkoniyatlar

- **Ko'p provider qo'llab-quvvatlash** -- Anthropic Claude, OpenAI GPT, Google Gemini, Groq. Provider ni config da almashtirasiz, kod o'zgarmaydi.
- **Avtomatik failover** -- bir nechta provider sozlang, xatolik bo'lsa Qanot o'zi boshqasiga o'tadi.
- **Jonli streaming** -- Telegram Bot API 9.5 `sendMessageDraft` orqali real-time streaming. `editMessageText` va blocked fallback ham bor.
- **RAG (Retrieval-Augmented Generation)** -- tayyor document indexing, gibrid qidiruv (vector + BM25). Lokal vector storage uchun sqlite-vec ishlatadi.
- **Xotira tizimi** -- WAL protocol har xabarni tekshiradi: tuzatishlar va afzalliklarni javob berishdan oldin skanerlaydi. Kunlik qaydlar, session holati, va uzoq muddatli xotira fayllari.
- **Context boshqaruv** -- token tracking, 60% da avtomatik compaction, 50% da working buffer.
- **Cron scheduler** -- APScheduler bilan rejalashtirilgan vazifalar, izolyatsiyalangan agent yoki system event rejimida.
- **Plugin tizimi** -- decorator-based plugin API orqali yangi toollar qo'shing.
- **Har user alohida** -- har bir Telegram user uchun alohida suhbat tarixi, bo'sh turganlar avtomatik o'chiriladi.
- **Ovoz qo'llab-quvvatlash** -- 4 ta voice provider (Muxlisa, KotibAI, Aisha, Whisper) -- nutqni matnga va matnni nutqqa.
- **Model routing** -- 3 bosqichli routing (Haiku/Sonnet/Opus), xabar murakkabligiga qarab -- narxni optimallashtirish.
- **Web dashboard** -- Bloomberg Terminal uslubidagi monitoring dashboard :8765 portda.
- **Rasm yaratish/tahrirlash** -- Gemini bilan tabiiy tilda rasm yaratish va tahrirlash.
- **Multi-agent delegatsiya** -- boshqa agentlarga vazifa topshirish, tool/model override bilan.
- **Ijro xavfsizligi** -- 3 daraja (open/cautious/strict) -- sandboxed buyruq bajarish.
- **Web qidiruv** -- Brave Search API, SSRF himoyali web fetch.
- **Narx kuzatuv** -- har user uchun token va narx statistikasi.
- **MCP klient** -- Model Context Protocol orqali 1000+ tashqi tool serverlariga ulanish.
- **Brauzer toollar** -- Playwright orqali sahifalarni ko'rish, bosish, forma to'ldirish, skrinshot olish.
- **Ko'nikmalar tizimi** -- agent takroriy vazifalar uchun qayta ishlatiladigan SKILL.md + skriptlar yaratadi, qayta ishga tushirmasdan hot-reload.
- **22 ta Telegram buyruqlar** -- inline tugma bilan sozlamalar boshqaruvi (model, ovoz, til, routing, xavfsizlik va boshqalar).
- **Anthropic xotira tooli** -- `/memories` papkasi va o'rnatilgan xotira xatti-harakati bilan ikki darajali arxitektura.
- **Server tomonida kod bajarish** -- Anthropic `code_execution_20250825` orqali sandboxed Python bajarish.
- **Webhook va WebChat** -- tashqi voqealar uchun webhook (GitHub, CRM, CI/CD) va WebSocket asosidagi webchat adapter.
- **Hayot sikli hooklari** -- on_startup, on_shutdown, on_pre_turn, on_post_turn kengaytirish nuqtalari.
- **1M kontekst oynasi** -- Opus 4.6 va Sonnet 4.6 uchun avtomatik aniqlash.
- **115+ plugin toollar** -- amoCRM, Bitrix24, 1C, AbsMarket, iBox POS, Eskiz SMS va boshqalar uchun tayyor pluginlar.

## OpenClaw bilan solishtirish

| Jihat | Qanot AI | OpenClaw |
|-------|----------|----------|
| Hajm | Yengil (~35 modul) | Og'ir (ko'p modullar) |
| Providerlar | 4 ta tayyor + failover | Odatda bitta provider |
| Streaming | Native `sendMessageDraft` | Faqat `editMessageText` |
| RAG | Tayyor gibrid qidiruv | Tashqi dependency |
| Xotira | WAL protocol + kunlik qaydlar + Anthropic xotira tooli | Oddiy xotira |
| Context | Avto-compaction + working buffer (1M oyna) | Qo'lda boshqarish |
| MCP | 1000+ tool server uchun tayyor klient | MCP qo'llab-quvvatlanmaydi |
| Brauzer | Playwright asosidagi browse/click/fill/screenshot | Brauzer toollari yo'q |
| Ko'nikmalar | Hot-reload skriptlari bilan o'z-o'zini yaxshilaydigan agent | Ko'nikmalar tizimi yo'q |
| Buyruqlar | 22 ta Telegram slash buyruqlari, inline tugmalar | Cheklangan buyruqlar |
| Bozor | O'zbekiston (timezone, Telegram) | Umumiy |

## Tez boshlash

```bash
# 1. O'rnatish
pip install qanot

# 2. Loyiha yaratish
qanot init mybot

# 3. Sozlash (bot_token va api_key yozing)
nano mybot/config.json

# 4. Ishga tushirish
qanot start mybot
```

Bot Telegram da ishga tushdi. Unga xabar yuboring.

## Hujjatlar

- [Boshlash](getting-started.md) -- o'rnatish, birinchi bot, config sozlash
- [Sozlash ma'lumotnomasi](configuration.md) -- barcha config maydonlari tushuntirilgan
- [LLM Providerlar](providers.md) -- provider sozlash, failover, maxsus providerlar
- [Xotira tizimi](memory.md) -- WAL protocol, kunlik qaydlar, working buffer
- [RAG](rag.md) -- document indexing, gibrid qidiruv, xotira integratsiyasi
- [Toollar](tools.md) -- tayyor toollar, cron toollar, RAG toollar
- [Plugin tizimi](plugins.md) -- maxsus toollar va pluginlar yaratish
- [Telegram integratsiya](telegram.md) -- javob rejimlari, streaming, webhook
- [Cron Scheduler](scheduler.md) -- rejalashtirilgan vazifalar, heartbeat, proaktiv xabarlar
- [Arxitektura](architecture.md) -- tizim dizayni, agent loop, ma'lumot oqimi
- [API ma'lumotnomasi](api-reference.md) -- klass va metod hujjatlari

## Talablar

- Python 3.11+
- Telegram bot token ([@BotFather](https://t.me/BotFather) dan)
- Kamida bitta LLM API key (Anthropic, OpenAI, Gemini, yoki Groq)
- Ixtiyoriy: RAG vector qidiruv uchun `sqlite-vec` (`pip install qanot[rag]`)
- Ixtiyoriy: MCP klient -- tashqi tool serverlarga ulanish (`pip install qanot[mcp]`)
- Ixtiyoriy: Brauzer toollar -- Playwright orqali (`pip install qanot[browser]`)
