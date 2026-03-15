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
- **115+ plugin toollar** -- amoCRM, Bitrix24, 1C, AbsMarket va boshqalar uchun tayyor pluginlar.

## OpenClaw bilan solishtirish

| Jihat | Qanot AI | OpenClaw |
|-------|----------|----------|
| Hajm | Yengil (~30 modul) | Og'ir (ko'p modullar) |
| Providerlar | 4 ta tayyor + failover | Odatda bitta provider |
| Streaming | Native `sendMessageDraft` | Faqat `editMessageText` |
| RAG | Tayyor gibrid qidiruv | Tashqi dependency |
| Xotira | WAL protocol + kunlik qaydlar | Oddiy xotira |
| Context | Avto-compaction + working buffer | Qo'lda boshqarish |
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
