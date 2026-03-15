# Arxitektura

Bu sahifa Qanot AI ning ichki tuzilishini tavsiflaydi: agent loop, ma'lumotlar oqimi va komponentlar qanday bog'lanadi.

## Tizim umumiy ko'rinishi

```
                         Telegram
                            |
                     TelegramAdapter
                      (aiogram 3.x)
                            |
                    +-------+-------+
                    |               |
                  Agent          CronScheduler
               (har bir user)    (APScheduler)
                    |               |
              +-----+-----+   spawn_isolated_agent()
              |     |     |
           Provider |  ToolRegistry
           (LLM)   |     |
              |    Context  +---> Built-in Tools
              |   Tracker   +---> Cron Tools
              |             +---> RAG Tools
              |             +---> Plugin Tools
              |
        +-----+-----+
        |     |     |
   Anthropic OpenAI Gemini Groq
        |     |     |     |
        +--FailoverProvider--+
```

## Ishga tushirish ketma-ketligi

`qanot/main.py` initsializatsiyani shu tartibda boshqaradi:

1. **Config yuklash** -- `load_config()` `config.json` ni o'qiydi
2. **Workspace initsializatsiyasi** -- `init_workspace()` birinchi ishga tushirishda shablonlarni ko'chiradi
3. **Provider yaratish** -- Yagona provider yoki ko'p provayderli `FailoverProvider`
4. **Context tracker yaratish** -- Sessiya uchun token kuzatuvi
5. **Tool registry yaratish** -- Bo'sh registry
6. **RAG engine initsializatsiyasi** (agar yoqilgan bo'lsa) -- Embedder, vector store, RAG engine yaratish; workspace xotira fayllarini indekslash
7. **Built-in tool'larni ro'yxatga olish** -- `read_file`, `write_file`, `list_files`, `run_command`, `web_search`, `memory_search`, `session_status`
8. **Session writer yaratish** -- JSONL log yozuvchi
9. **Cron scheduler yaratish** -- Tool registry havolasi bilan APScheduler
10. **Cron tool'larni ro'yxatga olish** -- `cron_create`, `cron_list`, `cron_update`, `cron_delete`
11. **Plugin'larni yuklash** -- Topish, import qilish, sozlash, plugin tool'larni ro'yxatga olish
12. **Agent yaratish** -- Provider, tool'lar, sessiya, kontekstni ulash
13. **RAG tool'larni ro'yxatga olish** -- `rag_index`, `rag_search`, `rag_list`, `rag_forget` (agent havolasi kerak)
14. **Memory hook'larni ro'yxatga olish** -- RAG indexer'ni memory write hodisalariga ulash
15. **Scheduler'ni ishga tushirish** -- Job'larni yuklash, APScheduler'ni ishga tushirish
16. **Telegram'ni ishga tushirish** -- Polling yoki webhook serverni boshlash

## Agent loop

Asosiy agent loop har bir user xabar uchun 25 ta iteratsiyagacha ishlaydi:

```
User xabar
    |
    v
WAL Protocol scan (tuzatishlar, afzalliklar, qarorlar)
    |
    v
Compaction recovery tekshiruvi (kerak bo'lsa working buffer inject qilish)
    |
    v
Xabarni suhbat tarixiga qo'shish
    |
    +---> [Loop boshlanishi: iteratsiya 1..25]
    |         |
    |     Proaktiv compaction tekshiruvi (agar > 60%, compact qilish)
    |         |
    |     Xabarlarni tuzatish (yetim tool_result'larni tuzatish)
    |         |
    |     Tizim prompt'ini yig'ish (workspace fayllardan)
    |         |
    |     LLM provider'ni chaqirish (vaqtinchalik xatolar uchun qayta urinish bilan)
    |         |
    |     Token ishlatilishini kuzatish
    |         |
    |     +--- stop_reason == "tool_use" ---+
    |     |                                  |
    |     |   Tool call loop'larini tekshirish |
    |     |   (3x bir xil chaqiruv, A-B-A-B)  |
    |     |        |                         |
    |     |   Tool'larni bajarish (30s timeout)|
    |     |        |                         |
    |     |   Natijalarni tarixga qo'shish   |
    |     |        |                         |
    |     |   [Loop davom]                   |
    |     |                                  |
    |     +--- stop_reason == "end_turn" ----+
    |     |                                  |
    |     |   Oxirgi matn javobi             |
    |     |   Sessiyaga loglash              |
    |     |   Working buffer'ga qo'shish     |
    |     |   Kunlik yozuv yozish            |
    |     |   [Javobni qaytarish]            |
    |     |                                  |
    |     +--- boshqa / max iteratsiyalar ---+
    |                                        |
    v                                        v
  Javob matni                            Xato xabar
```

### Streaming varianti

`run_turn_stream()` bir xil loop'ni kuzatadi, lekin `StreamEvent` obyektlarini yield qiladi:

- `text_delta` -- LLM dan matn qismi
- `tool_use` -- tool bajarilmoqda (ko'rsatish uchun matn yo'q)
- `done` -- to'liq `ProviderResponse` bilan oxirgi javob

Streaming variantida fallback bor: agar streaming vaqtinchalik xato bilan muvaffaqiyatsiz bo'lsa, bir marta streaming'siz `chat()` bilan qayta urinadi.

## Har bir foydalanuvchi izolatsiyasi

Har bir Telegram foydalanuvchi izolyatsiya qilingan suhbat holatiga ega:

```python
Agent._conversations: dict[str | None, list[dict]]
#   kalit: user_id string (yoki cron job'lar uchun None)
#   qiymat: xabar tarixi ro'yxati

Agent._locks: dict[str | None, asyncio.Lock]
#   har bir foydalanuvchi uchun yozish xavfsizligi lock'i

Agent._last_active: dict[str | None, float]
#   bo'sh turishni aniqlash uchun monotonic vaqt belgisi
```

- Turli foydalanuvchilarning xabarlari hech qachon aralashmaydi
- Har bir foydalanuvchi uchun lock'lar bir xil foydalanuvchidan xabarlarning bir vaqtda qayta ishlanishini oldini oladi
- 1 soatdan (3600s) ko'proq bo'sh turgan suhbatlar avtomatik o'chiriladi
- Cron job'lar o'zlarining izolyatsiya qilingan suhbatlari uchun `None` ni user_id sifatida ishlatadi

## Tizim prompt yig'ish

`build_system_prompt()` prompt'ni workspace fayllardan yig'adi:

```
1. SOUL.md          -- Asosiy shaxsiyat va ko'rsatmalar
2. IDENTITY.md      -- Agent nomi, uslubi, emoji afzalliklari
3. SKILL.md         -- Proaktiv agent xatti-harakatlari
4. TOOLS.md         -- Tool hujjatlari
5. *_TOOLS.md       -- Plugin tool hujjatlari
6. AGENTS.md        -- Ish qoidalari
7. SESSION-STATE.md -- WAL yozuvlari (faol sessiya konteksti)
8. USER.md          -- Inson konteksti
9. BOOTSTRAP.md     -- Birinchi ishga tushirish marosimi (agar fayl mavjud bo'lsa)
+ Tool call uslub qoidalari (hardcoded)
+ Sessiya ma'lumotlari (sana, vaqt, kontekst %, tokenlar)
```

**Minimal rejim** (cron isolated agent'lar uchun): Faqat SOUL.md + TOOLS.md + sessiya ma'lumotlari.

**Belgi byudjeti:**
- Har bir fayl uchun: maksimal 20,000 belgi (70% bosh / 20% quyruq qirqish)
- Umumiy prompt: maksimal 150,000 belgi

`{date}`, `{bot_name}`, `{owner_name}`, `{timezone}` o'zgaruvchilari oxirgi prompt'da almashtiriladi.

## Streaming pipeline

```
LLM Provider
    |
    | yields StreamEvent(type="text_delta", text="...")
    v
Agent.run_turn_stream()
    |
    | yields StreamEvent chaqiruvchiga
    v
TelegramAdapter._respond_stream()
    |
    | matnni yig'adi
    | flush_interval da draft yuboradi
    v
Bot.sendMessageDraft(chat_id, draft_id, text)
    |
    | final
    v
Bot.sendMessage(chat_id, formatted_html)
```

Muhim nuqtalar:
- Tool bajarilayotganda draft yangilanishlari to'xtatiladi — race condition'larni oldini oladi
- Telegram adapter ortiqcha yangilanishlarni oldini olish uchun oxirgi yuborilgan draft matnini kuzatadi
- Har bir streaming sessiya noyob `draft_id` oladi
- Oxirgi xabar HTML formatlash bilan yuboriladi (Markdown konvertatsiya qilinadi)

## Xato boshqaruvi va failover oqimi

```
Agent provider.chat() ni chaqiradi
    |
    +--- Muvaffaqiyat --> javobni qaytarish
    |
    +--- Exception ushlandi
    |        |
    |    classify_error(e) --> error_type
    |        |
    |    +--- PERMANENT (auth, billing)
    |    |       --> darhol raise qilish
    |    |
    |    +--- TRANSIENT (rate_limit, overloaded, timeout)
    |    |       --> eksponensial backoff bilan qayta urinish (2s, 4s, max 30s)
    |    |       --> 2 ta qayta urinishgacha
    |    |
    |    +--- UNKNOWN
    |            --> darhol raise qilish
    |
    [Barcha qayta urinishlar muvaffaqiyatsiz bo'lsa]
        |
        +--- rate_limit --> "Limitga yetdik..."
        +--- auth       --> "API kalitda xatolik..."
        +--- billing    --> "API hisob muammosi..."
        +--- boshqa     --> "Xatolik yuz berdi..."
```

`FailoverProvider` bilan oqim kengayadi:

```
FailoverProvider.chat()
    |
    Faol provider'ni sinash
    |     |
    |   Muvaffaqiyat --> mark_success(), qaytarish
    |     |
    |   Muvaffaqiyatsizlik --> classify_error(), mark_failed()
    |                   |
    |               cooldown = 120s * failure_count (max 600s)
    |                   |
    Keyingi mavjud provider'ni sinash
    |     ...
    |
    Barcha provider'lar tugadi --> oxirgi xatoni raise qilish
```

## Kontekst boshqaruvi oqimi

```
Navbat N: input_tokens = 45,000 / 200,000 max (22.5%)
    --> Oddiy ish

Navbat N+5: input_tokens = 100,000 (50%)
    --> Working buffer FAOLLASHADI
    --> Almashuvlar working-buffer.md ga loglanadi

Navbat N+10: taxminiy keyingi = 128,000 (64% > 60% chegarasi)
    --> Proaktiv compaction ishga tushadi
    --> Xabarlar: [birinchi 2] + [xulosa belgisi] + [oxirgi 4]
    --> Token taxmini ~35% ga tushiriladi

Navbat N+20: xabarlarda compaction aniqlandi
    --> Tiklash konteksti inject qilinadi:
        - working-buffer.md
        - SESSION-STATE.md
        - bugungi kunlik yozuvlar
```

## Sessiya loglash

Har bir xabar almashuvi sessions papkasidagi JSONL fayllariga loglanadi:

```
sessions/
├── 2025-01-15.jsonl     # Oddiy suhbatlar
├── cron-heartbeat-20250115-160000.jsonl  # Cron job sessiyalari
```

Har bir satr JSON obyekti:

```json
{
  "type": "message",
  "id": "msg_000001",
  "parentId": "",
  "timestamp": "2025-01-15T10:30:00+00:00",
  "message": {"role": "user", "content": "Hello"},
}
```

Assistant xabarlari ishlatilish statistikasi va model ma'lumotlarini o'z ichiga oladi. Fayl yozuvlari krossplatforma qulflashni ishlatadi (Unix'da `fcntl.LOCK_EX`, Windows'da graceful degradation).

## Ma'lumotlar oqimi xulosasi

| Ma'lumot | Kim yozadi | Kim o'qiydi |
|----------|-----------|-------------|
| `config.json` | Foydalanuvchi | `load_config()` |
| `SOUL.md`, `TOOLS.md`, va boshqalar | Foydalanuvchi / Agent / Plugin'lar | `build_system_prompt()` |
| `SESSION-STATE.md` | WAL protocol | Tizim prompt, `memory_search` |
| `memory/*.md` (kunlik yozuvlar) | Agent loop | `memory_search`, RAG indexer |
| `MEMORY.md` | Agent (tool'lar orqali) | `memory_search`, RAG indexer |
| `memory/working-buffer.md` | Context tracker | Compaction tiklash |
| `sessions/*.jsonl` | Session writer | Tashqi monitoring tool'lar |
| `cron/jobs.json` | Cron tool'lar / Foydalanuvchi | Cron scheduler |
| `rag.db` | RAG engine | RAG search |
| `uploads/*` | Telegram adapter | Agent (`read_file` orqali) |

## Modul ma'lumotnomasi

Yuqorida tavsiflangan asosiy modullardan tashqari, Qanot quyidagi qo'shimcha komponentlarni o'z ichiga oladi:

### Asosiy modullar

| Modul | Vazifasi |
|-------|---------|
| `agent.py` | Asosiy agent loop (25 iteratsiya, circuit breaker, natijaga yo'naltirilgan loop'lar) |
| `agent_bot.py` | Alohida agent bot runtime |
| `backup.py` | Ishga tushirishdagi backup funksionallik |
| `config.py` | JSON config yuklovchi, `Config` dataclass, `SecretRef` |
| `context.py` | Token kuzatuvi, 50% buffer, 60% compaction chegarasi |
| `compaction.py` | Ko'p bosqichli LLM xulosalanishi (OpenClaw uslubida) |
| `routing.py` | 3 bosqichli model routing (Haiku/Sonnet/Opus) |
| `voice.py` | Voice provider integratsiyasi (Muxlisa, KotibAI, Aisha, Whisper) |
| `ratelimit.py` | Har bir foydalanuvchi uchun sliding window rate limiter |
| `links.py` | Avtomatik URL preview inject qilish |
| `utils.py` | Yordamchi funksiyalar (qirqish, helper'lar) |
| `fs_safe.py` | Xavfsiz fayl yozish (tizim papkalarni bloklash, symlink tekshirish) |
| `secrets.py` | SecretRef resolver (env var'lar, fayllar) |
| `session.py` | JSONL append-only sessiya loglash (krossplatforma qulflash) |
| `prompt.py` | Tizim prompt yig'uvchi (9 bo'lim + MEMORY.md inject) |
| `telegram.py` | aiogram 3.x adapter (stream/partial/blocked + inline tugmalar) |
| `dashboard.py` | :8765 portdagi web dashboard server (aiohttp) |
| `dashboard_html.py` | Dashboard HTML (Bloomberg Terminal estetikasi) |
| `daemon.py` | Krossplatforma daemon (systemd/launchd/schtasks) |
| `scheduler.py` | APScheduler cron (isolated + systemEvent rejimlari) |
| `cli.py` | CLI: init/start/stop/restart/status/config/update/doctor |

### Tool modullari (`tools/`)

| Modul | Vazifasi |
|-------|---------|
| `builtin.py` | read/write/list/run_command/send_file/memory/session/cost |
| `cron.py` | 4 ta cron boshqaruv tool'lari |
| `web.py` | web_search (Brave) + web_fetch (SSRF himoyalangan) |
| `image.py` | generate_image + edit_image (Gemini) |
| `rag.py` | 4 ta RAG tool (search/index/list/forget) |
| `delegate.py` | Ko'p agentli delegatsiya (delegate/converse/spawn) |
| `subagent.py` | Sub-agent boshqaruvi |
| `agent_manager.py` | agent'larni create/update/delete/restart qilish |
| `doctor.py` | Tizim diagnostikasi |
| `workspace.py` | Workspace init + shablonlar |
| `jobs_io.py` | Cron jobs JSON I/O yordamchi funksiyalar |
