# Toollar

Qanot AI da agent suhbat davomida chaqira oladigan tayyor toollar bor. Toollar orqali agent fayl tizimi, web, xotira, cron, RAG, rasm yaratish, multi-agent delegatsiya va diagnostika bilan ishlaydi.

## Toollar qanday ishlaydi

Agent loop shunday ishlaydi:

1. LLM prompt da tool ta'riflarini ko'radi
2. `tool_use` bloklar bilan qaysi tool ni qanday parametrlar bilan chaqirishni aytadi
3. Qanot tool ni bajaradi va natijani qaytaradi
4. LLM natijani qayta ishlaydi -- yana tool chaqiradi yoki userga javob beradi

Har bir tool bajarilishi 120 soniya timeout ga ega (tool ga qarab sozlash mumkin). 50,000 belgidan oshgan natijalar qisqartiriladi.

## Tayyor toollar

### read_file

Workspace yoki absolyut yo'ldan fayl o'qiydi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `path` | string | Ha | Fayl yo'li (workspace ga nisbatan yoki absolyut) |

```json
{"path": "notes/todo.md"}
```

Fayl mazmunini matn sifatida qaytaradi. 50,000 belgidan oshgan fayllar umumiy hajm ko'rsatilgan holda qisqartiriladi.

### write_file

Faylga yozadi, kerak bo'lsa papkalarni yaratadi. Yo'llar `fs_safe.validate_write_path()` orqali tekshiriladi -- tizim papkalariga yozish va symlink hujumlar bloklanadi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `path` | string | Ha | Fayl yo'li (workspace ga nisbatan yoki absolyut) |
| `content` | string | Ha | Fayl mazmuni |

```json
{"path": "notes/todo.md", "content": "# TODO\n\n- Buy groceries"}
```

Natija: `{"success": true, "path": "...", "bytes": 123}`.

### list_files

Berilgan yo'ldagi fayl va papkalarni ko'rsatadi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `path` | string | Yo'q | Papka yo'li (default: workspace root) |

```json
{"path": "notes/"}
```

`name`, `type` ("file" yoki "dir"), va `size` bilan JSON massiv qaytaradi.

### run_command

Workspace papkasida shell buyruq bajaradi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `command` | string | Ha | Shell buyruq (pipe, redirect, `&&` ishlaydi) |
| `timeout` | integer | Yo'q | Timeout soniyalarda (default: 120, maks: 120) |
| `cwd` | string | Yo'q | Ish papkasi (default: workspace) |
| `approved` | boolean | Yo'q | User tasdiqlashi (cautious rejim uchun) |

```json
{"command": "python3 script.py"}
```

**Xavfsizlik:** `exec_security` orqali 3 bosqichli xavfsizlik modeli:

- **`open`** -- Faqat xavfli patternlar bloklisti amal qiladi. `rm -rf /`, `mkfs`, `dd`, fork bomblar va hujum toollari har doim bloklanadi.
- **`cautious`** (standart) -- Xavfli patternlar bloklanadi, bundan tashqari riskli buyruqlar (pip install, curl, sudo, git push, docker, database klientlar va h.k.) user tasdiqlashini talab qiladi. User rad etsa, buyruq bekor qilinadi.
- **`strict`** -- Faqat `exec_allowlist` dagi buyruqlarga (prefix match) ruxsat beriladi. Qolganlari bloklanadi.

Buyruqlar 120 soniyadan keyin timeout bo'ladi. Chiqish 50,000 belgiga cheklanadi.

### memory_search

Agent xotira fayllaridan qidiradi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `query` | string | Ha | Qidiruv so'rovi |

```json
{"query": "database password"}
```

RAG yoqilgan bo'lsa, semantik vector qidiruv ishlatadi. Aks holda MEMORY.md, kunlik qaydlar (oxirgi 30 ta), va SESSION-STATE.md bo'ylab katta-kichik harfga befarq substring qidiruv. Natijalar 50 taga cheklangan.

### session_status

Joriy session statistikasi -- context foydalanish va narx.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| (yo'q) | -- | -- | Parametr yo'q |

```json
{}
```

Natija:

```json
{
  "context_percent": 23.5,
  "total_input_tokens": 45000,
  "total_output_tokens": 12000,
  "total_tokens": 57000,
  "max_tokens": 200000,
  "buffer_active": false,
  "turn_count": 8,
  "last_prompt_tokens": 45000,
  "user_cost": {"input_tokens": 45000, "output_tokens": 12000, "cost_usd": 0.12},
  "total_cost": 1.45
}
```

### cost_status

Har user uchun token va narx statistikasi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `user_id` | string | Yo'q | So'rov qilinadigan user ID (default: joriy user) |

```json
{"user_id": "123456"}
```

Har user uchun token soni, narx taqsimoti va umumiy summani qaytaradi. `user_id` berilmasa, joriy user statistikasini ko'rsatadi.

### send_file

Workspace dan faylni Telegram orqali userga yuboradi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `path` | string | Ha | Fayl yo'li (workspace ga nisbatan yoki absolyut) |

```json
{"path": "generated/report.pdf"}
```

Natija: `{"success": true, "path": "...", "size": 12345}`. Fayllar Telegram adapter orqali yuboriladi. Maks fayl hajmi 50 MB (Telegram cheklovi).

## Web toollar

### web_search

Brave Search API orqali internetdan qidiradi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `query` | string | Ha | Qidiruv so'rovi (maks 2000 belgi) |
| `count` | integer | Yo'q | Natijalar soni (1-10, default: 5) |

```json
{"query": "Python asyncio tutorial", "count": 5}
```

JSON qaytaradi: so'rov, natijalar soni, va har birida `title`, `url`, `description`, ixtiyoriy `age` bor massiv. Natijalar 15 daqiqa keshlanadi (50 tagacha). Config da `brave_api_key` kerak.

### web_fetch

Web sahifadan o'qiladigan mazmun oladi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `url` | string | Ha | Olinadigan URL (http:// yoki https://) |
| `max_chars` | integer | Yo'q | Maks chiqish belgilari (default: 50,000) |

```json
{"url": "https://docs.python.org/3/library/asyncio.html"}
```

JSON qaytaradi: `url`, `final_url`, `title`, `content` (ajratilgan matn), `content_type`, `length`, va manba eslatmasi. HTML sahifalar soddalashtirilgan markdown ga aylantiriladi. JSON javoblar chiroyli formatlanadi.

**SSRF himoya:** URL lar quyidagilarga qarshi tekshiriladi:
- Bloklangan hostlar (localhost, metadata.google.internal)
- Bloklangan portlar (SSH, SMTP, database portlari, Docker daemon va h.k.)
- Shaxsiy/ajratilgan IP tarmoqlar (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, link-local, IPv6 loopback)
- DNS resolve tekshiriladi -- hostname-to-private-IP redirect larni ushlash uchun
- Redirect manzillar qayta tekshiriladi (maks 3 redirect)
- Javob tanasi 2 MB ga cheklangan
- 30 soniya timeout

## Rasm toollar

`gemini_api_key` sozlanganda mavjud. Gemini rasm yaratish bilan ishlaydi.

### generate_image

Matn tavsifidan yangi rasm yaratadi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `prompt` | string | Ha | Yaratiladigan rasmning batafsil matnli tavsifi |
| `model` | string | Yo'q | Rasm modeli (default: `gemini-3-pro-image-preview`) |

Qo'llab-quvvatlanadigan modellar:
- `gemini-3-pro-image-preview` -- Nano Banana Pro (eng yuqori sifat)
- `gemini-3.1-flash-image-preview` -- Nano Banana 2 (tez)
- `gemini-2.5-flash-image` -- Nano Banana (tezlik uchun optimallashtirilgan)

```json
{"prompt": "A serene mountain landscape at sunset with a lake reflection"}
```

Natija: `{"status": "ok", "image_path": "...", "model": "...", "description": "...", "size_bytes": 123456}`. Rasm `workspace/generated/` ga saqlanadi va Telegram orqali userga avtomatik yuboriladi.

### edit_image

Userning oxirgi yuborgan rasmini matn ko'rsatmasi asosida tahrirlaydi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `prompt` | string | Ha | Tahrirlash ko'rsatmasi (masalan, "qora-oq qil") |
| `model` | string | Yo'q | Rasm modeli (generate_image bilan bir xil variantlar) |

```json
{"prompt": "Remove the background and replace with mountains"}
```

Tool suhbat tarixidan orqaga qarab oxirgi user rasmini qidiradi. Rasm topilmasa, avval rasm yuborishni so'raydi. Tahrirlangan rasm saqlanadi va userga yuboriladi.

## Cron toollar

Bu toollar agent ga rejalashtirilgan vazifalarni yaratish va boshqarish imkonini beradi. Cron joblar qanday bajarilishi haqida [Scheduler](scheduler.md) ga qarang.

### cron_create

Yangi rejalashtirilgan vazifa yoki bir martalik eslatma yaratadi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `name` | string | Ha | Noyob vazifa nomi (maks 200 belgi) |
| `schedule` | string | Yo'q* | Cron ifodasi (masalan, `0 */4 * * *`) |
| `at` | string | Yo'q* | Bir martalik eslatma uchun ISO 8601 vaqt belgisi (masalan, `2026-03-12T17:00:00+05:00`) |
| `prompt` | string | Ha | Eslatma matni yoki vazifa prompt (maks 10,000 belgi) |
| `mode` | string | Yo'q | `isolated` (toollar bilan to'liq agent) yoki `systemEvent` (faqat matn yetkazish). Default: `systemEvent` |
| `delete_after_run` | boolean | Yo'q | Bajarilgandan keyin avto-o'chirish (default: `at` eslatmalari uchun true) |
| `timezone` | string | Yo'q | IANA timezone (masalan, `Asia/Tashkent`) |

*`schedule` yoki `at` dan biri majburiy.

```json
{
  "name": "daily-summary",
  "schedule": "0 20 * * *",
  "prompt": "Write a summary of today's conversations and save to MEMORY.md",
  "mode": "isolated"
}
```

Yaratilgandan keyin scheduler avtomatik qayta yuklanadi.

### cron_list

Barcha rejalashtirilgan vazifalarni ko'rsatadi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| (yo'q) | -- | -- | Parametr yo'q |

To'liq jobs.json mazmunini qaytaradi.

### cron_update

Mavjud vazifani yangilaydi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `name` | string | Ha | Yangilanadigan vazifa nomi |
| `schedule` | string | Yo'q | Yangi cron ifodasi |
| `mode` | string | Yo'q | Yangi bajarish rejimi (`systemEvent` yoki `isolated`) |
| `prompt` | string | Yo'q | Yangi prompt |
| `enabled` | boolean | Yo'q | Yoqish/o'chirish |

### cron_delete

Rejalashtirilgan vazifani o'chiradi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `name` | string | Ha | O'chiriladigan vazifa nomi |

## RAG toollar

`rag_enabled: true` va mos embedding provider mavjud bo'lganda ishlaydi. To'liq hujjatlar uchun [RAG](rag.md) ga qarang.

### rag_index

Faylni RAG tizimiga indekslaydi. Bir xil faylni qayta indekslash oldingi chunklarni almashtiradi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `path` | string | Ha | Fayl yo'li (.txt, .md, .csv, .pdf) -- workspace ichida bo'lishi kerak |
| `name` | string | Yo'q | Ko'rsatiladigan nom / manba identifikatori (default: fayl nomi) |

```json
{"path": "docs/handbook.pdf", "name": "Employee Handbook"}
```

Natija: `{"indexed": true, "source": "Employee Handbook", "chunks": 42}`. PDF uchun PyMuPDF kerak (`pip install PyMuPDF`). Workspace dan tashqariga chiqish oldini olinadi.

### rag_search

Indekslangan hujjatlardan gibrid semantik + kalit so'z qidiruv.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `query` | string | Ha | Qidiruv so'rovi (maks 10,000 belgi) |
| `top_k` | integer | Yo'q | Natijalar soni (1-100, default: 5) |

```json
{"query": "vacation policy", "top_k": 3}
```

`text`, `source`, va `score` (0-1, yuqori = yaxshi) bilan natijalar massivini qaytaradi.

### rag_list

Barcha indekslangan hujjat manbalarini chunk sonlari bilan ko'rsatadi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| (yo'q) | -- | -- | Parametr yo'q |

### rag_forget

Hujjat manbasini RAG indeksdan o'chiradi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `source` | string | Ha | O'chiriladigan manba nomi |

```json
{"source": "Employee Handbook"}
```

Natija: `{"deleted": true, "source": "...", "chunks_removed": 42}`.

## Delegatsiya toollar

Multi-agent hamkorlik toollari. Asosiy agent boshqa agentlarga vazifa topshiradi, ko'p navbatli suhbatlar olib boradi, va natijalarni project board orqali ulashadi.

### delegate_to_agent

Bir martalik vazifa delegatsiyasi -- vazifani boshqa agentga topshiradi va natijani kutadi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `task` | string | Ha | Vazifa tavsifi (batafsil) |
| `agent_id` | string | Ha | Maqsad agent identifikatori |
| `context` | string | Yo'q | Vazifaga tegishli qo'shimcha kontekst (maks 4,000 belgi) |

```json
{
  "task": "Write SEO-optimized meta descriptions for these 5 product pages",
  "agent_id": "seo-expert",
  "context": "Target market is Uzbekistan, write in Uzbek language"
}
```

Agent natijasini qaytaradi (maks 8,000 belgi). 120 soniya timeout. Maks delegatsiya chuqurligi 2 (agentlar boshqa agentlarga delegatsiya qila oladi, lekin cheksiz emas). Sikl aniqlash aylanma delegatsiyalarni oldini oladi. Natijalar umumiy project board ga joylashtiriladi.

### converse_with_agent

Boshqa agent bilan ko'p navbatli suhbat (5 gacha navbat).

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `message` | string | Ha | Agentga yuboriladigan xabar |
| `agent_id` | string | Ha | Maqsad agent identifikatori |
| `max_turns` | integer | Yo'q | Maks suhbat navbatlari (1-5, default: 3) |

```json
{
  "message": "Let's design the database schema for a blog platform",
  "agent_id": "architect",
  "max_turns": 5
}
```

To'liq suhbat transkriptini qaytaradi. Agentlar orasida hamkorlik va muzokaralar uchun foydali.

### view_project_board

Umumiy project board -- barcha agentlar bajargan ishlar natijalarini ko'rish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `agent_id` | string | Yo'q | Agent ID bo'yicha filtrlash |

```json
{"agent_id": "seo-expert"}
```

`agent_id`, `task`, `result`, va `timestamp` bilan yozuvlar massivini qaytaradi. Har user uchun maks 20 yozuv. 6 soat harakatsizlikdan keyin ma'lumotlar o'chiriladi.

### clear_project_board

Umumiy project board ni tozalaydi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| (yo'q) | -- | -- | Parametr yo'q |

### list_agents

Barcha mavjud agentlarni model, roli va imkoniyatlari bilan ko'rsatadi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| (yo'q) | -- | -- | Parametr yo'q |

`id`, `name`, `model`, `prompt` (qisqartirilgan), va `tools_allow`/`tools_deny` bilan agent ta'riflari massivini qaytaradi.

### agent_session_history

Boshqa agentning suhbat transkriptini o'qish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `agent_id` | string | Ha | Tarixi o'qiladigan agent |

Agentning oxirgi 20 xabarini qaytaradi: rol, mazmun, vaqt belgisi, va toollar ishlatilganmi.

### agent_sessions_list

Barcha faol agent sessionlarini metadata bilan ko'rsatadi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| (yo'q) | -- | -- | Parametr yo'q |

Har bir faol agent uchun session ma'lumoti: xabar soni, oxirgi faollik vaqti.

### view_agent_activity

Agent faollik logini ko'rish -- barcha agent interaksiyalarini real-time monitoring.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `limit` | integer | Yo'q | Maks qaytariladigan yozuvlar (default: 20, maks: 50) |
| `agent_id` | string | Yo'q | Agent ID bo'yicha filtrlash |

Delegatsiya hodisalari, natijalar, xatolar va vaqt ma'lumotlarini qaytaradi.

### set_monitor_group

Real-time agent monitoring uchun Telegram guruh sozlash.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `group_id` | integer | Ha | Telegram guruh ID (manfiy raqam, masalan, -1001234567890) |

O'rnatilganda, agent-agent interaksiyalari bu guruhga forward qilinadi -- real-time kuzatish mumkin. Har bir agent boti guruhga qo'shilgan bo'lishi kerak.

## Sub-Agent toollar

### spawn_sub_agent

Murakkab, uzoq davom etadigan vazifalar uchun izolyatsiyalangan background sub-agent ishga tushiradi. Sub-agent mustaqil ishlaydi va tugagandan keyin natijani Telegram orqali userga yetkazadi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `task` | string | Ha | Vazifa tavsifi -- batafsil va mustaqil (maks 10,000 belgi) |

```json
{"task": "Research Python vs Rust performance benchmarks -- use web_search to find recent comparisons and summarize findings with sources"}
```

Natija: `{"status": "spawned", "task_id": "abc12345", "message": "..."}`. Sub-agent 5 daqiqa timeout ga ega. Har user uchun maks 3 ta bir vaqtda ishlaydigan sub-agent. Mavjud toollar: web_search, web_fetch, read_file, memory_search.

### list_sub_agents

Joriy user uchun faol sub-agentlarni ko'rsatadi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| (yo'q) | -- | -- | Parametr yo'q |

Natija: `{"active": 2, "agents": [{"task_id": "abc12345", "status": "running"}, ...]}`.

## Agent boshqaruv toollar

Dinamik agent hayot sikli boshqaruvi -- botni qayta ishga tushirmasdan runtime da agent yaratish, yangilash va o'chirish.

### create_agent

O'z shaxsiyati, modeli va ixtiyoriy ravishda o'z Telegram boti bilan yangi agent yaratadi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `id` | string | Ha | Agent ID (kichik harflar + raqamlar + tire, maks 32 belgi) |
| `name` | string | Ha | Agent ko'rsatiladigan nomi |
| `prompt` | string | Yo'q | Agent shaxsiyati va ko'rsatmalar (berilmasa avto-yaratiladi) |
| `model` | string | Yo'q | LLM model (default: asosiy agent modeli) |
| `provider` | string | Yo'q | LLM provider (default: asosiy agent provideri) |
| `bot_token` | string | Yo'q | Telegram bot token -- berilsa, mustaqil Telegram bot sifatida ishga tushadi |
| `tools_allow` | array[string] | Yo'q | Ruxsat berilgan toollar (bo'sh = barcha toollar) |
| `tools_deny` | array[string] | Yo'q | Taqiqlangan toollar |
| `timeout` | integer | Yo'q | Timeout soniyalarda (default: 120) |

```json
{
  "id": "seo-expert",
  "name": "SEO Mutaxassis",
  "prompt": "You are an SEO expert specializing in the Uzbekistan market...",
  "model": "claude-haiku-4-5"
}
```

Agentlar config.json ga saqlanadi va qayta ishga tushirmasdan hot-launch qilinadi. `bot_token` berilsa, agent mustaqil Telegram bot sifatida ishlaydi. Token bo'lmasa, `delegate_to_agent` orqali foydalaniladigan ichki agent bo'ladi. `workspace/agents/<id>/` da SOUL.md fayl yaratiladi.

### update_agent

Mavjud agentning config ni yangilaydi. Faqat o'zgartirmoqchi bo'lgan maydonlarni bering.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `id` | string | Ha | Yangilanadigan agent ID |
| `name` | string | Yo'q | Yangi ko'rsatiladigan nom |
| `prompt` | string | Yo'q | Yangi shaxsiyat/ko'rsatmalar (SOUL.md ham yangilanadi) |
| `model` | string | Yo'q | Yangi LLM model |
| `provider` | string | Yo'q | Yangi LLM provider |
| `bot_token` | string | Yo'q | Yangi Telegram bot token (bo'sh string token ni olib tashlaydi va botni to'xtatadi) |
| `tools_allow` | array[string] | Yo'q | Yangi ruxsat berilgan toollar |
| `tools_deny` | array[string] | Yo'q | Yangi taqiqlangan toollar |
| `timeout` | integer | Yo'q | Yangi timeout |

```json
{"id": "seo-expert", "model": "claude-opus-4-6"}
```

Natija: `{"status": "updated", "agent_id": "...", "changes": ["model"]}`.

### delete_agent

Agentni o'chiradi. Telegram boti ishlayotgan bo'lsa, to'xtatiladi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `id` | string | Ha | O'chiriladigan agent ID |

```json
{"id": "seo-expert"}
```

Natija: `{"status": "deleted", "agent_id": "...", "bot_stopped": true}`.

### restart_self

Butun bot jarayonini qayta ishga tushiradi. Config o'zgarishlaridan keyin, yangi agent yaratilgandan keyin yoki xatolikdan tiklash uchun foydali. Bot o'ziga SIGTERM yuboradi va service manager (systemd/launchd) qayta ishga tushiradi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `reason` | string | Yo'q | Qayta ishga tushirish sababi |

```json
{"reason": "Applied new configuration"}
```

Darhol natija qaytaradi. Bot 2 soniyadan keyin qayta ishga tushadi.

## Doctor tool

### doctor

Tizim salomatligi diagnostikasi. 7 ta quyi tizimni tekshiradi va har biri uchun holat (ok/warning/error) beradi.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| (yo'q) | -- | -- | Parametr yo'q |

```json
{}
```

Tekshirishlar:
- **config** -- bot_token, API keylar, workspace_dir yozish ruxsati, sessions_dir, kerakli workspace fayllar (SOUL.md, TOOLS.md, IDENTITY.md)
- **memory** -- MEMORY.md o'qilishi va hajmi, SESSION-STATE.md hajmi (>100KB da ogohlantiradi), kunlik qaydlar soni (30 kun), memory/ papka hajmi
- **context** -- Joriy context foydalanish %, token sonlari, buffer holati, compaction rejimi
- **provider** -- Bitta yoki ko'p provider rejimi, model nomlari
- **rag** -- RAG database mavjudligi va hajmi, FTS5 mavjudligi, embedding kesh yozuvlari
- **sessions** -- Sessions papka hajmi, yaqindagi session fayllar soni (7 kun), oxirgi session vaqt belgisi
- **disk** -- Workspace hajmi, mavjud disk xotirasi (<100MB da ogohlantiradi)

Natija:

```json
{
  "status": "healthy",
  "checks": {
    "config": {"status": "ok", "details": "..."},
    "memory": {"status": "ok", "details": "..."},
    "context": {"status": "ok", "details": "..."},
    "provider": {"status": "ok", "details": "..."},
    "rag": {"status": "ok", "details": "..."},
    "sessions": {"status": "ok", "details": "..."},
    "disk": {"status": "ok", "details": "..."}
  },
  "warnings": [],
  "timestamp": "2026-03-16T12:00:00+00:00"
}
```

## Brauzer toollar

`pip install qanot[browser]` o'rnatilganda mavjud. Playwright orqali web sahifalar bilan ishlaydi.

### browse_url

URL ni ochish va sahifa mazmunini olish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `url` | string | Ha | Ochiladigan web sahifa URL |
| `wait_for` | string | Yo'q | Kutish uchun CSS selector (default: sahifa yuklanganda) |

```json
{"url": "https://example.com"}
```

Sahifa mazmunini HTML yoki soddalashtirilgan matn sifatida qaytaradi.

### click_element

Sahifadagi elementni bosish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `selector` | string | Ha | CSS selector (masalan, `"button.submit"`, `"#login"`) |

```json
{"selector": "button.submit"}
```

### fill_form

Forma maydonlarini to'ldirish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `fields` | object | Ha | Maydon nomlari va qiymatlari (masalan, `{"username": "admin", "password": "123"}`) |

```json
{"fields": {"email": "user@example.com", "message": "Salom"}}
```

### screenshot

Sahifaning skrinshotini olish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `selector` | string | Yo'q | Faqat ma'lum elementning skrinshoti (default: to'liq sahifa) |

```json
{}
```

Skrinshot `workspace/generated/` ga saqlanadi va userga Telegram orqali yuboriladi.

### extract_data

Sahifadan strukturalangan ma'lumotlarni ajratish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `selector` | string | Ha | Ajratiladigan elementlar uchun CSS selector |
| `attributes` | array[string] | Yo'q | Ajratiladigan atributlar (default: matn mazmuni) |

```json
{"selector": "table.prices tr", "attributes": ["innerText"]}
```

## Ko'nikma toollar

Agent o'z-o'zini yaxshilash uchun qayta ishlatiladigan ko'nikmalar yaratadi va boshqaradi.

### create_skill

Yangi ko'nikma yaratish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `name` | string | Ha | Ko'nikma nomi |
| `description` | string | Ha | Ko'nikma tavsifi |
| `rules` | string | Yo'q | SKILL.md ga qo'shiladigan qoidalar |
| `script` | string | Yo'q | `workspace/skills/` ga saqlanadigan skript mazmuni |

```json
{
  "name": "weekly-report",
  "description": "Haftalik hisobot yaratish",
  "rules": "Har juma kechqurun haftalik hisobot yoz",
  "script": "#!/bin/bash\ndate >> report.md"
}
```

### list_skills

Barcha ko'nikmalarni ko'rsatish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| (yo'q) | -- | -- | Parametr yo'q |

### run_skill_script

Ko'nikma skriptini bajarish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `name` | string | Ha | Bajariladigan ko'nikma nomi |

```json
{"name": "weekly-report"}
```

### delete_skill

Ko'nikmani o'chirish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `name` | string | Ha | O'chiriladigan ko'nikma nomi |

## Xotira toollar

Anthropic xotira tooli (`memory_20250818`) ikki darajali arxitekturani ta'minlaydi.

### memories_view

Xotira fayllarini ko'rish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `path` | string | Yo'q | Ko'riladigan xotira fayli yo'li (default: barcha fayllar) |

### memories_create

Yangi xotira fayli yaratish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `path` | string | Ha | Xotira fayli yo'li (`memories/` papkasida) |
| `content` | string | Ha | Fayl mazmuni |

### memories_str_replace

Xotira faylida matnni almashtirish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `path` | string | Ha | Xotira fayli yo'li |
| `old_str` | string | Ha | Eski matn |
| `new_str` | string | Ha | Yangi matn |

### memories_insert

Xotira fayliga ma'lum joyga matn qo'shish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `path` | string | Ha | Xotira fayli yo'li |
| `insert_line` | int | Ha | Kiritish qatori raqami |
| `content` | string | Ha | Kiritiladigan matn |

### memories_delete

Xotira faylini o'chirish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `path` | string | Ha | O'chiriladigan xotira fayli yo'li |

### memories_rename

Xotira faylining nomini o'zgartirish.

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `old_path` | string | Ha | Joriy fayl yo'li |
| `new_path` | string | Ha | Yangi fayl yo'li |

Barcha xotira fayllari `workspace/memories/` papkasida saqlanadi. RAG tizimi bu papkani avtomatik indekslaydi.

## Maxsus tool yaratish

Maxsus toollar [plugin tizimi](plugins.md) orqali qo'shiladi. Tez bir martalik toollar uchun `ToolRegistry` ga to'g'ridan-to'g'ri ro'yxatga olish ham mumkin:

```python
from qanot.agent import ToolRegistry

registry = ToolRegistry()

async def my_tool(params: dict) -> str:
    name = params.get("name", "world")
    return f"Hello, {name}!"

registry.register(
    name="greet",
    description="Greet someone by name.",
    parameters={
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "description": "Name to greet"},
        },
    },
    handler=my_tool,
)
```

Tool handlerlar `dict` parametr qabul qilib `str` qaytaradigan async funksiyalar bo'lishi kerak. Strukturalangan ma'lumotlar uchun JSON odatiy format. Xatolar uchun exception tashlang -- ular ushlanib `{"error": "..."}` sifatida qaytariladi.

## Tool xavfsizligi

- **3 bosqichli buyruq xavfsizligi:** `run_command` `exec_security` (open/cautious/strict) orqali qaysi buyruqlarga ruxsat berilishini nazorat qiladi. `rm -rf /`, fork bomblar va hujum toollari har doim bloklanadi.
- **Fayl yozish tekshiruvi:** `write_file` yo'llarni `fs_safe.validate_write_path()` bilan tekshiradi -- tizim papkalari bloklanadi, symlinklar tekshiriladi.
- **SSRF himoya:** `web_fetch` URL larni shaxsiy tarmoqlar, bloklangan portlar va ichki hostlarga qarshi tekshiradi. DNS resolve tekshiriladi, redirect manzillar qayta tekshiriladi.
- **Timeout:** Buyruq bajarilishi 120 soniyada timeout. Web fetch 30 soniyada timeout.
- **Natija qisqartirish:** Katta natijalar 50,000 belgiga qisqartiriladi -- context shishishini oldini oladi.
- **Sikl aniqlash:** Agent loop takroriy bir xil tool chaqiruvlarni aniqlaydi (3 ta ketma-ket yoki navbatlashadigan patternlar) va siklni userga xabar bilan to'xtatadi.
- **Deterministik xato maslahatlar:** "not found" yoki "permission denied" kabi xatolarda LLM ga bir xil parametrlar bilan qayta urinmaslikni aytadigan maslahat beriladi.
- **Delegatsiya cheklovlar:** Maks chuqurlik 2, sikl aniqlash, 120 soniya timeout, natija hajmi chegaralari.
- **Sub-agent cheklovlar:** Har user uchun maks 3 ta bir vaqtda, 5 daqiqa timeout.
- **Fayl hajmi cheklovlar:** `send_file` Telegram ning 50 MB yuklash chegarasini nazorat qiladi.
