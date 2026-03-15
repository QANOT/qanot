# Qanot AI v2.0 foydalanuvchi qo'llanmasi

Telegram botlar uchun yengil Python agent framework. O'zbekiston bozori uchun yaratilgan.

---

## Mundarija

1. [Tez boshlash](#1-tez-boshlash)
2. [Konfiguratsiya](#2-konfiguratsiya)
3. [Provider'lar](#3-provayderlar)
4. [Ovoz](#4-ovoz)
5. [Model routing](#5-model-routing)
6. [Xavfsizlik](#6-xavfsizlik)
7. [Xotira](#7-xotira)
8. [RAG](#8-rag)
9. [Ko'p agentli tizim](#9-kop-agentli-tizim)
10. [Dashboard](#10-dashboard)
11. [CLI ma'lumotnomasi](#11-cli-malumotnomasi)
12. [Muammolarni hal qilish](#12-muammolarni-hal-qilish)

---

## 1. Tez boshlash

Uch qadam bilan ishlaydigan bot.

### 1-qadam: O'rnatish

```bash
pip install qanot
```

### 2-qadam: Initsializatsiya

```bash
qanot init
```

Interaktiv sozlash sihrbozi sizni quyidagilar bo'yicha yo'naltiradi:

- Telegram bot token ([@BotFather](https://t.me/BotFather) dan)
- AI provider tanlash (Anthropic, OpenAI, Gemini, Groq yoki Ollama)
- API kalit tekshiruvi
- Ovoz qo'llab-quvvatlash (ixtiyoriy)
- Web search (ixtiyoriy)

U `config.json`, standart `SOUL.md` bilan `workspace/` papkasini va `sessions/`, `cron/`, `plugins/` papkalarini yaratadi.

### 3-qadam: Ishga tushirish

```bash
qanot start
```

Bot o'zini OS xizmati sifatida o'rnatadi (macOS'da launchd, Linux'da systemd) va fon rejimida ishga tushadi. Birinchi xabar yuborgan odam egasi bo'ladi.

Oldingi rejimda ishga tushirish (Docker yoki debugging uchun foydali):

```bash
qanot start -f
```

---

## 2. Konfiguratsiya

Barcha konfiguratsiya `config.json` da joylashadi. `qanot init` sihrbozi uni yaratadi, lekin qo'lda ham tahrirlash mumkin.

### To'liq config ma'lumotnomasi

```jsonc
{
  // ── Asosiy ──
  "bot_token": "123456:ABC...",        // @BotFather dan Telegram bot token
  "provider": "anthropic",             // Asosiy provider: anthropic|openai|gemini|groq
  "model": "claude-sonnet-4-6",        // Asosiy model
  "api_key": "sk-ant-...",             // Asosiy API kalit

  // ── Ko'p provayderli (ixtiyoriy) ──
  "providers": [                       // Failover uchun qo'shimcha provider'lar
    {
      "name": "anthropic-main",
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "api_key": "sk-ant-..."
    },
    {
      "name": "gemini-backup",
      "provider": "gemini",
      "model": "gemini-2.5-flash",
      "api_key": "AIza..."
    }
  ],

  // ── Identifikatsiya ──
  "owner_name": "Sirojiddin",          // Ismingiz (bot suhbatda ishlatadi)
  "bot_name": "Qanot",                 // Bot shaxsi nomi
  "timezone": "Asia/Tashkent",         // IANA timezone

  // ── Yo'llar ──
  "workspace_dir": "/data/workspace",  // SOUL.md, MEMORY.md, kunlik yozuvlar
  "sessions_dir": "/data/sessions",    // JSONL sessiya loglari
  "cron_dir": "/data/cron",            // Rejali vazifalar (jobs.json)
  "plugins_dir": "/data/plugins",      // Plugin papkalari

  // ── Kontekst ──
  "max_context_tokens": 200000,        // Maksimal kontekst oynasi (modelga bog'liq)
  "compaction_mode": "safeguard",      // "safeguard" = 60% da auto-compact
  "max_memory_injection_chars": 4000,  // RAG/compaction'dan inject qilinadigan maks belgilar
  "history_limit": 50,                 // Qayta ishga tushirishda tiklanadigan maks navbatlar

  // ── Telegram ──
  "response_mode": "stream",           // "stream"|"partial"|"blocked"
  "stream_flush_interval": 0.8,        // Draft yangilanishlari orasidagi soniyalar (stream rejimi)
  "telegram_mode": "polling",          // "polling"|"webhook"
  "webhook_url": "",                   // telegram_mode "webhook" bo'lsa kerak
  "webhook_port": 8443,                // Webhook server uchun lokal port
  "max_concurrent": 4,                 // Maksimal bir vaqtda xabar qayta ishlash

  // ── Kirish nazorati ──
  "allowed_users": [],                 // Telegram user ID'lar (bo'sh = ommaviy)

  // ── Ovoz ──
  "voice_provider": "muxlisa",         // "muxlisa"|"kotib"|"aisha"|"whisper"
  "voice_api_key": "",                 // Standart ovoz API kaliti (fallback)
  "voice_api_keys": {                  // Provider bo'yicha kalitlar
    "muxlisa": "",
    "kotib": ""
  },
  "voice_mode": "inbound",             // "off"|"inbound"|"always"
  "voice_name": "",                    // TTS ovoz nomi
  "voice_language": "",                // STT tilini majburlash (uz/ru/en), bo'sh = auto

  // ── RAG ──
  "rag_enabled": true,                 // RAG orqali xotira qidiruvini yoqish
  "rag_mode": "auto",                  // "auto"|"agentic"|"always"

  // ── Web Search ──
  "brave_api_key": "",                 // Brave Search API kaliti (bepul: 2000 so'rov/oy)

  // ── UX ──
  "reactions_enabled": false,          // Xabarlarga emoji reaktsiyalar yuborish
  "reply_mode": "coalesced",           // "off"|"coalesced"|"always"
  "group_mode": "mention",              // "off"|"mention"|"all"

  // ── O'z-o'zini tiklash ──
  "heartbeat_enabled": true,           // Davriy o'z-o'zini tekshirishni yoqish
  "heartbeat_interval": "0 */4 * * *", // Har 4 soatda

  // ── Kunlik brifing ──
  "briefing_enabled": true,            // Ertalabki xulosa
  "briefing_schedule": "0 8 * * *",    // Har kuni 8:00 da

  // ── Kengaytirilgan fikrlash (Claude) ──
  "thinking_level": "off",             // "off"|"low"|"medium"|"high"
  "thinking_budget": 10000,            // Maksimal fikrlash tokenlari

  // ── Bajarish xavfsizligi ──
  "exec_security": "open",             // "open"|"cautious"|"strict"
  "exec_allowlist": [],                // strict rejimda ruxsat berilgan buyruqlar

  // ── Model routing ──
  "routing_enabled": false,            // Oddiy xabarlarni arzonroq modelga yo'naltirish
  "routing_model": "claude-haiku-4-5-20251001",  // Salomlashishlar uchun arzon model
  "routing_mid_model": "claude-sonnet-4-6",       // O'rta darajali model
  "routing_threshold": 0.3,            // Murakkablik chegarasi (0.0-1.0)

  // ── Rasm generatsiyasi ──
  "image_api_key": "",                 // Rasm generatsiyasi uchun Gemini kaliti
  "image_model": "gemini-3-pro-image-preview",

  // ── Dashboard ──
  "dashboard_enabled": true,           // :8765 portda web UI
  "dashboard_port": 8765,

  // ── Backup ──
  "backup_enabled": true,              // Ishga tushirishda avtomatik backup

  // ── Ko'p agentli ──
  "agents": [],                        // Agent ta'riflari (Ko'p agentli bo'limga qarang)
  "monitor_group_id": 0,               // Agent monitoring uchun Telegram guruh ID

  // ── Plugin'lar ──
  "plugins": []                        // Plugin konfiguratsiyalari
}
```

### Init'dan keyin config'ni o'zgartirish

```bash
# Joriy config'ni ko'rsatish
qanot config show

# Qiymatni o'zgartirish
qanot config set model claude-opus-4-6
qanot config set response_mode partial
qanot config set exec_security cautious

# Zaxira provider qo'shish (interaktiv)
qanot config add-provider

# O'zgarishlar kuchga kirishi uchun qayta ishga tushirish
qanot restart
```

---

## 3. Provider'lar

Qanot beshta AI provider'ni qo'llab-quvvatlaydi. Failover uchun bir nechta provider sozlash mumkin.

### Anthropic (Claude)

Standart API kalitlar va OAuth tokenlarni (Claude Code'dan) qo'llab-quvvatlaydi.

**Standart API kalit:**

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-api03-..."
}
```

**OAuth token (Claude Code):**

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-oat01-..."
}
```

OAuth tokenlar avtomatik Claude Code identifikatsiya headerlarini yoqadi — bu Opus va Sonnet modellariga kirish imkonini beradi. Provider `sk-ant-oat` prefiksini aniqlaydi va headerlarni shunga mos sozlaydi.

**Mavjud modellar:**

| Model | Qachon ishlatish |
|---|---|
| `claude-sonnet-4-6` | Tez, ko'p vazifalar uchun tavsiya etiladi |
| `claude-opus-4-6` | Eng qobiliyatli, murakkab fikrlash uchun |
| `claude-haiku-4-5-20251001` | Eng arzon, oddiy so'rovlar uchun |

**Kengaytirilgan fikrlash:**

```json
{
  "thinking_level": "medium",
  "thinking_budget": 10000
}
```

Darajalar: `off`, `low`, `medium`, `high`. Faqat Anthropic bilan ishlaydi.

### OpenAI (GPT)

```json
{
  "provider": "openai",
  "model": "gpt-4.1",
  "api_key": "sk-proj-..."
}
```

**Mavjud modellar:**

| Model | Qachon ishlatish |
|---|---|
| `gpt-4.1` | Eng yangi, tavsiya etiladi |
| `gpt-4.1-mini` | Tez va arzon |
| `gpt-4o` | Ko'p modal |
| `gpt-4o-mini` | Eng arzon |

### Google Gemini

```json
{
  "provider": "gemini",
  "model": "gemini-2.5-flash",
  "api_key": "AIza..."
}
```

Gemini ichki OpenAI-mos endpoint ishlatadi (`https://generativelanguage.googleapis.com/v1beta/openai/`). RAG uchun bepul embedding ham taqdim etadi.

**Mavjud modellar:**

| Model | Qachon ishlatish |
|---|---|
| `gemini-2.5-flash` | Tez, tavsiya etiladi |
| `gemini-2.5-pro` | Eng qobiliyatli |
| `gemini-2.0-flash` | Eng arzon |

### Groq

```json
{
  "provider": "groq",
  "model": "llama-3.3-70b-versatile",
  "api_key": "gsk_..."
}
```

Groq OpenAI-mos API ishlatadi (`https://api.groq.com/openai/v1`).

**Mavjud modellar:**

| Model | Qachon ishlatish |
|---|---|
| `llama-3.3-70b-versatile` | Tavsiya etiladi |
| `llama-3.1-8b-instant` | Eng tez |
| `qwen/qwen3-32b` | Qwen 3 |

### Ollama (lokal)

Bepul va maxfiy. Lokal ishlaydi, API kalit shart emas.

```json
{
  "providers": [
    {
      "name": "ollama-main",
      "provider": "openai",
      "model": "qwen3.5:35b",
      "api_key": "ollama",
      "base_url": "http://localhost:11434/v1"
    }
  ]
}
```

Ollama `"provider": "openai"` ishlatishiga e'tibor bering — chunki u OpenAI-mos API gaplashadi. Framework Ollama'ni `base_url` dagi `11434` port orqali aniqlaydi va avtomatik:

- Qwen modellari uchun thinking rejimni o'chiradi (30x tezroq)
- `think=false` bilan native Ollama API ishlatadi
- VRAM ziddiyatini oldini olish uchun RAG embedding uchun FastEmbed (CPU) ishlatadi

**Ollama'ni o'rnatish va ishga tushirish:**

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3.5:35b
```

### Ko'p provayderli failover

Bir nechta provider sozlang. Qanot avval asosiyni sinaydi, keyin zaxiraga o'tadi:

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key": "sk-ant-...",
  "providers": [
    {
      "name": "anthropic-main",
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "api_key": "sk-ant-..."
    },
    {
      "name": "gemini-backup",
      "provider": "gemini",
      "model": "gemini-2.5-flash",
      "api_key": "AIza..."
    }
  ]
}
```

---

## 4. Ovoz

Qanot ovozdan matnga (STT) va matndan ovozga (TTS) uchun to'rtta voice provider'ni qo'llab-quvvatlaydi.

### Ovoz rejimlari

| Rejim | Xatti-harakat |
|---|---|
| `off` | Ovozli xabarlar e'tiborsiz qoldiriladi |
| `inbound` | Kiruvchi ovozni transkripsiya qiladi; mos bo'lganda ovoz bilan javob beradi |
| `always` | Har doim ovoz bilan javob beradi |

### Muxlisa.uz (standart)

Mahalliy o'zbek provider'i. OGG ni to'g'ridan-to'g'ri qabul qiladi (STT uchun ffmpeg konvertatsiya shart emas).

```json
{
  "voice_provider": "muxlisa",
  "voice_api_keys": {
    "muxlisa": "your-muxlisa-api-key"
  },
  "voice_mode": "inbound",
  "voice_name": "maftuna"
}
```

**Ovozlar:** `maftuna`, `asomiddin`

API kalitni [muxlisa.uz](https://muxlisa.uz) dan oling.

### KotibAI

6 ta ovoz, ko'p tilli qo'llab-quvvatlash, avtomatik til aniqlash.

```json
{
  "voice_provider": "kotib",
  "voice_api_keys": {
    "kotib": "your-jwt-token"
  },
  "voice_mode": "inbound",
  "voice_name": "aziza"
}
```

**Ovozlar:** `aziza`, `sherzod` va yana 4 ta

JWT tokenni [developer.kotib.ai](https://developer.kotib.ai) dan oling.

### Aisha AI

STT + TTS kayfiyat aniqlash bilan. O'zbek, ingliz va rus tillarini qo'llab-quvvatlaydi.

```json
{
  "voice_provider": "aisha",
  "voice_api_keys": {
    "aisha": "your-aisha-api-key"
  },
  "voice_mode": "inbound"
}
```

**Ovozlar:** `Gulnoza`, `Jaxongir`

API kalitni [aisha.group](https://aisha.group) dan oling.

### OpenAI Whisper

Faqat STT (TTS yo'q). Yuqori aniqlik, 50+ til.

```json
{
  "voice_provider": "whisper",
  "voice_api_keys": {
    "whisper": "sk-proj-..."
  },
  "voice_mode": "inbound"
}
```

API kalitni [platform.openai.com](https://platform.openai.com) dan oling.

### Bir nechta voice provider ishlatish

Bir nechta provider sozlash va har biriga alohida kalit berish mumkin:

```json
{
  "voice_provider": "muxlisa",
  "voice_api_keys": {
    "muxlisa": "key-for-muxlisa",
    "kotib": "jwt-for-kotib",
    "whisper": "sk-proj-for-whisper"
  },
  "voice_mode": "inbound"
}
```

### STT tilini majburlash

Standart holda til avtomatik aniqlanadi. Muayyan tilni majburlash uchun:

```json
{
  "voice_language": "uz"
}
```

Variantlar: `uz`, `ru`, `en` yoki avtomatik aniqlash uchun bo'sh qoldiring.

---

## 5. Model routing

3 bosqichli model routing oddiy xabarlarni arzonroq modellarga yuborib pul tejaydi.

### Qanday ishlaydi

1. **1-bosqich (arzon):** Salomlashishlar, rahmatlar, oddiy savollar `routing_model` ga (masalan, Haiku)
2. **2-bosqich (o'rta):** Oddiy suhbat `routing_mid_model` ga (masalan, Sonnet)
3. **3-bosqich (to'liq):** Murakkab vazifalar, tool ishlatish, fikrlash asosiy `model` da qoladi

Agent har bir kiruvchi xabarni murakkablik bo'yicha baholaydi (0.0 dan 1.0 gacha). `routing_threshold` dan past xabarlar arzon bosqichga yo'naltiriladi.

### Konfiguratsiya

```json
{
  "routing_enabled": true,
  "routing_model": "claude-haiku-4-5-20251001",
  "routing_mid_model": "claude-sonnet-4-6",
  "routing_threshold": 0.3,
  "model": "claude-opus-4-6"
}
```

### Xarajat tejash misollari

| Xabar | Murakkablik | Qayerga yo'naltiriladi |
|---|---|---|
| "Salom!" | 0.05 | Haiku ($0.80/MTok) |
| "Bugun ob-havo qanday?" | 0.15 | Haiku |
| "Loyiha strukturasini tushuntir" | 0.45 | Sonnet ($3/MTok) |
| "Bu kodni refaktor qil va testlar yoz" | 0.85 | Opus ($15/MTok) |

Routing yoqilganda, odatiy botlar 40-60% xarajat kamayishini ko'rishadi.

---

## 6. Xavfsizlik

### Bajarish xavfsizlik rejimlari

`run_command` tool'i shell buyruqlarini bajarish uchun uchta xavfsizlik darajasiga ega.

#### open (standart)

Faqat xavfli buyruqlar bloklanadi (rm -rf /, fork bomb'lar, disk to'ldirish hujumlari va h.k.). Qolgan hamma narsa erkin ishlaydi.

```json
{
  "exec_security": "open"
}
```

#### cautious

Xavfli buyruqlar bloklanadi. Xatarli buyruqlar (pip install, curl, sudo, docker, git push va h.k.) inline tugma yoki matnli tasdiqlash orqali foydalanuvchi ruxsatini talab qiladi.

```json
{
  "exec_security": "cautious"
}
```

Bot so'raydi: "Bu buyruqni bajarishga ruxsat berasizmi: `pip install requests`?" va tasdiqlashni kutadi.

#### strict

Faqat oq ro'yxatdagi buyruqlarga ruxsat beriladi. Qolganlarning barchasi rad etiladi.

```json
{
  "exec_security": "strict",
  "exec_allowlist": [
    "git status",
    "git log",
    "git diff",
    "python",
    "ls",
    "cat"
  ]
}
```

Oq ro'yxat yozuvlari prefiks bo'yicha mos keladi: `"git"` `git status`, `git log` va boshqalarga ruxsat beradi.

### Har doim bloklangan buyruqlar

Bular barcha rejimlarda (shu jumladan `open`) bloklanadi:

- `rm -rf /` va variantlari (root/home'ni rekursiv o'chirish)
- `mkfs`, `dd`, `shred` (diskni buzish)
- `shutdown`, `reboot`, `poweroff`
- `chmod 777 /`, `chown root`
- Tarmoq hujum tool'lari (nmap, sqlmap, hydra, metasploit)
- `curl | sh`, `wget | sh` (masofaviy kod bajarish)
- Fork bomb'lar, disk to'ldirish hujumlari

### Rate limiting

Parallel xabar qayta ishlashni cheklash uchun `max_concurrent` dan foydalaning:

```json
{
  "max_concurrent": 4
}
```

Botni kim ishlata olishini cheklash uchun `allowed_users` dan foydalaning:

```json
{
  "allowed_users": [123456789, 987654321]
}
```

Bo'sh massiv botni ommaviy qiladi. Birinchi xabar yuborgan foydalanuvchi avtomatik egasi bo'ladi.

### SecretRef

API kalitlarni hech qachon `config.json` da ochiq matn sifatida saqlamang. Ularni muhit o'zgaruvchilari yoki fayllardan yuklash uchun SecretRef dan foydalaning.

**Muhit o'zgaruvchisidan:**

```json
{
  "api_key": {"env": "ANTHROPIC_API_KEY"},
  "bot_token": {"env": "TELEGRAM_BOT_TOKEN"},
  "brave_api_key": {"env": "BRAVE_API_KEY"}
}
```

**Fayldan:**

```json
{
  "api_key": {"file": "/run/secrets/anthropic_key"},
  "bot_token": {"file": "/run/secrets/bot_token"}
}
```

Faylga asoslangan sirlar xavfsizlik tekshiruvlariga ega:
- Symlink'lar rad etiladi (escape hujumlarini oldini oladi)
- Barchaga o'qish mumkin fayllar ogohlantirish chiqaradi (`chmod 600` tavsiya etiladi)
- Maksimal 64 KB fayl hajmi

SecretRef quyidagilar uchun ishlaydi: `api_key`, `bot_token`, `brave_api_key`, `voice_api_key`, `image_api_key`, barcha provider `api_key` maydonlari va `voice_api_keys` qiymatlari.

### Fayl qafasi (File Jail)

`write_file` tool'i tizim papkalariga yozishni bloklaydi:

- `/etc`, `/usr`, `/bin`, `/sbin`, `/lib`, `/boot`, `/proc`, `/sys`, `/dev`
- `/System`, `/Library` (macOS)
- `C:\Windows`, `C:\Program Files` (Windows)

Papka traversalini oldini olish uchun symlink yozuvlari ham bloklanadi.

---

## 7. Xotira

Qanot'da botga suhbatlar orasida o'rganish va eslab qolish imkonini beradigan uch bosqichli xotira tizimi bor.

### WAL Protocol (Write-Ahead Log)

Har bir user xabar LLM javob berishdan OLDIN skanerlanadi. WAL quyidagilarni aniqlaydi:

| Kategoriya | Trigger misollari (inglizcha) | Trigger misollari (o'zbekcha) |
|---|---|---|
| Tuzatishlar | "actually", "no, I meant" | "aslida", "to'g'ri emas" |
| Atoqli otlar | "my name is Ahmad" | "mening ismim Ahmad" |
| Afzalliklar | "I like Python", "I prefer dark mode" | "men Python yoqtiraman" |
| Qarorlar | "let's use React" | "keling React ishlataylik" |
| Aniq qiymatlar | URL'lar, sanalar, katta raqamlar | Xuddi shunday |
| Eslab qolish | "remember this", "don't forget" | "eslab qol", "unutma" |

Aniqlangan yozuvlar `SESSION-STATE.md` ga (faol ishchi xotira) yoziladi. Doimiy faktlar (ismlar, afzalliklar, aniq "eslab qol" so'rovlari) `MEMORY.md` ga ham saqlanadi.

### Fayl tuzilishi

```
workspace/
  MEMORY.md           # Uzoq muddatli saralangan faktlar
  SESSION-STATE.md    # Faol sessiya ishchi xotirasi (WAL yozuvlari)
  memory/
    2026-03-15.md     # Kunlik yozuvlar (suhbat xulosalari)
    2026-03-14.md
    ...
```

### MEMORY.md

Bot har doim eslab turishi kerak bo'lgan faktlarning uzoq muddatli saqlash joyi. Doimiy kategoriyalar uchun WAL tomonidan avtomatik yoziladi va compaction (xotira tozalash) davomida agent tomonidan yoziladi.

Misol:

```markdown
# MEMORY.md - Long-Term Memory

## Auto-captured

- **proper_noun**: [user:123] mening ismim Ahmad
- **preference**: [user:123] I prefer Python over JavaScript
- **remember**: [user:123] remember this: project deadline is March 30
```

### Kunlik yozuvlar

Kontekst compact qilinganda, agent suhbat xulosalarini `memory/YYYY-MM-DD.md` ga saqlaydi. Bular `memory_search` tool orqali qidirish mumkin va RAG tomonidan indekslanadi.

### Kontekst compaction

Kontekst 60% ishlatilganda working buffer faollashadi. Kontekst to'lib ketganda, Qanot:

1. Xotira tozalash ishga tushiradi (agent muhim faktlarni fayllarga saqlaydi)
2. Suhbatni xulosaleydi
3. Eski xabarlarni xulosa bilan almashtiradi
4. Yangi kontekst bilan davom etadi

`compaction_mode: "safeguard"` sozlamasi buni avtomatik ta'minlaydi.

### Agent qanday o'rganadi

```
User xabar --> WAL scan --> Faktlarni aniqlash --> SESSION-STATE.md / MEMORY.md ga yozish
                                                ↓
LLM javob --> Tool chaqiruvlar --> Kunlik yozuvlar --> memory/2026-03-15.md
                                                ↓
Kontekst to'lishi --> Compact --> Xulosa --> Inject qilingan xotiralar bilan yangi kontekst
                                                ↓
Keyingi xabar --> RAG search --> Tegishli xotiralarni inject qilish --> LLM to'liq kontekstni ko'radi
```

---

## 8. RAG

RAG (Retrieval-Augmented Generation) botning xotira qidiruvini semantik tushunish bilan kuchaytiradi.

### Qanday ishlaydi

Qanot gibrid qidiruvni qo'llaydi:

1. **FTS5** (SQLite to'liq matn qidiruv) -- kalit so'z mosligi, har doim mavjud
2. **Vektor embedding'lar** -- semantik o'xshashlik, embedding provider kerak

Natijalar reciprocal rank fusion va vaqt bo'yicha so'nish (yaqindagi xotiralar yuqoriroq) yordamida birlashtiriladi.

### Embedding provider zanjiri

Qanot mavjud config'dan eng yaxshi embedder'ni avtomatik aniqlaydi. Qo'shimcha API kalit shart emas.

| Ustuvorlik | Provider | O'lchamlar | Narx | Qachon ishlatiladi |
|---|---|---|---|---|
| 0 | FastEmbed (CPU) | 768 | Bepul | Ollama sozlashlari (VRAM ziddiyati yo'q) |
| 1 | Gemini | 3072 | Bepul daraja | Gemini API kaliti mavjud bo'lganda |
| 2 | OpenAI | 1536 | $0.02/MTok | OpenAI API kaliti mavjud bo'lganda |
| - | Faqat FTS | - | Bepul | Embedder mavjud bo'lmaganda fallback |

### Konfiguratsiya

RAG standart holda yoqilgan. Gemini yoki OpenAI kalitingiz bo'lsa, qo'shimcha konfiguratsiya shart emas.

```json
{
  "rag_enabled": true,
  "rag_mode": "auto"
}
```

**RAG rejimlari:**

| Rejim | Xatti-harakat |
|---|---|
| `auto` | Tegishli bo'lganda RAG avtomatik qidiradi |
| `agentic` | Agent `memory_search` tool orqali qachon qidirish kerakligini hal qiladi |
| `always` | Har doim RAG natijalarini kontekstga inject qiladi |

### Gemini embedding sozlash

Gemini API kalitingiz bo'lsa (hatto asosiy bo'lmagan provider sifatida), embedding'lar avtomatik ishlaydi:

```json
{
  "providers": [
    {
      "name": "gemini-embed",
      "provider": "gemini",
      "model": "gemini-2.5-flash",
      "api_key": "AIza..."
    }
  ]
}
```

Framework `gemini-embedding-001` (3072 o'lcham) ni o'zgarmagan kontentni qayta embedding qilishni oldini olish uchun embedding keshi bilan ishlatadi.

### Ollama embedding sozlash

Ollama foydalanuvchilari uchun CPU asosidagi embedding uchun FastEmbed'ni o'rnating (VRAM ziddiyatini oldini oladi):

```bash
pip install fastembed
```

Agar FastEmbed o'rnatilmagan bo'lsa, Ollama `nomic-embed-text` modeli orqali o'z embedding'iga qaytadi.

### Nima indekslanadi

- `MEMORY.md` -- uzoq muddatli faktlar
- `SESSION-STATE.md` -- faol sessiya holati
- `memory/*.md` -- kunlik yozuvlar (oxirgi 30 ta fayl)

Fayllar kontenti o'zgarganda qayta indekslanadi (hash asosidagi deduplikatsiya).

---

## 9. Ko'p agentli tizim

Qanot uchta o'zaro ta'sir shakli bilan ko'p agentli hamkorlikni qo'llab-quvvatlaydi.

### Agent ta'rifi

Agent'larni `config.json` da aniqlang:

```json
{
  "agents": [
    {
      "id": "deep-researcher",
      "name": "Chuqur Tadqiqotchi",
      "prompt": "You are a deep research agent. Use web_search and web_fetch extensively. Investigate topics thoroughly with multiple sources. Always cite your sources.",
      "model": "claude-opus-4-6",
      "bot_token": "",
      "tools_allow": ["web_search", "web_fetch", "memory_search", "read_file"],
      "tools_deny": [],
      "delegate_allow": [],
      "max_iterations": 15,
      "timeout": 180
    },
    {
      "id": "fast-coder",
      "name": "Tezkor Dasturchi",
      "prompt": "You are a fast coding agent. Write clean, working code quickly. Follow existing project conventions.",
      "model": "claude-haiku-4-5-20251001",
      "bot_token": "",
      "tools_deny": ["web_search", "web_fetch"],
      "delegate_allow": ["deep-researcher"],
      "timeout": 60
    }
  ]
}
```

### Agent maydonlari

| Maydon | Tavsif |
|---|---|
| `id` | Noyob identifikator (masalan, `"researcher"`, `"coder"`) |
| `name` | Inson o'qiy oladigan nom (masalan, `"Tadqiqotchi"`) |
| `prompt` | Tizim prompt / shaxsiyat |
| `model` | Model almashtirish (bo'sh = asosiy modelni ishlatish) |
| `provider` | Provider almashtirish (bo'sh = asosiy provider'ni ishlatish) |
| `api_key` | API kalit almashtirish (bo'sh = asosiy kalitni ishlatish) |
| `bot_token` | Alohida Telegram bot token (bo'sh = faqat ichki agent) |
| `tools_allow` | Tool oq ro'yxati (bo'sh = barcha tool'larga ruxsat) |
| `tools_deny` | Tool qora ro'yxati |
| `delegate_allow` | Bu agent qaysi boshqa agent'larga delegatsiya qila olishi (bo'sh = barchasi) |
| `max_iterations` | Maksimal tool-use loop'lar (standart: 15) |
| `timeout` | Timeout oldidan soniyalar (standart: 120) |

### O'zaro ta'sir shakllari

#### 1. delegate_to_agent

Bir martalik vazifa topshirish. Asosiy agent vazifa yuboradi, sub-agent uni bajaradi va natijani qaytaradi.

```
User: "Bu mavzu haqida chuqur tadqiqot qil"
Bot: [deep-researcher agent'ga delegatsiya qiladi]
     [deep-researcher web search'lar ishlatadi, manbalarni o'qiydi]
     [topilmalarni asosiy agent'ga qaytaradi]
Bot: "Tadqiqot natijalari: ..."
```

#### 2. converse_with_agent

Agent'lar orasida ko'p navbatli ping-pong suhbat (5 ta navbatgacha). Agent'lar yechim ustida ishlashi kerak bo'lganda foydali.

#### 3. spawn_sub_agent

Mustaqil ishlaydigan fon agent'ini yaratadi. Natijalar umumiy loyiha taxtasiga joylashtiriladi.

### Umumiy loyiha taxtasi

Agent'lar boshqa agent'lar o'qiy oladigan umumiy taxtaga natijalar joylashtirilishi mumkin. Bu to'g'ridan-to'g'ri xabar almashishsiz asinxron hamkorlikni ta'minlaydi.

### Agent monitoring

Barcha agent suhbatlarini monitoring uchun Telegram guruhga ko'chirish:

```json
{
  "monitor_group_id": -1001234567890
}
```

### Kirish nazorati

Qaysi agent'lar bir-biri bilan gaplasha olishini boshqarish uchun `delegate_allow` dan foydalaning:

```json
{
  "agents": [
    {
      "id": "coder",
      "delegate_allow": ["researcher"]
    },
    {
      "id": "researcher",
      "delegate_allow": []
    }
  ]
}
```

Bu yerda `coder` `researcher` ga delegatsiya qila oladi, lekin `researcher` hech kimga delegatsiya qila olmaydi. Cheksiz loop'larni oldini olish uchun maksimal delegatsiya chuqurligi 2.

---

## 10. Dashboard

Qanot real-time monitoring uchun web dashboard o'z ichiga oladi.

### Yoqish

```json
{
  "dashboard_enabled": true,
  "dashboard_port": 8765
}
```

Dashboard bot bilan birga avtomatik ishga tushadi. `http://localhost:8765` da kiring.

### API endpoint'lar

| Endpoint | Tavsif |
|---|---|
| `GET /` | Web UI (HTML dashboard) |
| `GET /api/status` | Bot holati: uptime, kontekst %, token soni, faol suhbatlar |
| `GET /api/config` | Joriy konfiguratsiya (sirlarsiz) |
| `GET /api/costs` | Har bir foydalanuvchi uchun xarajat kuzatuvi |
| `GET /api/memory` | Xotira fayllar ro'yxati |
| `GET /api/memory/{filename}` | Muayyan xotira faylini o'qish |
| `GET /api/tools` | Ro'yxatga olingan tool'lar ro'yxati |
| `GET /api/routing` | Model routing statistikasi |

### Misol: bot holatini tekshirish

```bash
curl http://localhost:8765/api/status
```

```json
{
  "bot_name": "Qanot",
  "model": "claude-sonnet-4-6",
  "provider": "anthropic",
  "uptime": "2h 15m 30s",
  "context_percent": 23.5,
  "total_tokens": 45200,
  "turn_count": 12,
  "api_calls": 28,
  "buffer_active": false,
  "active_conversations": 3
}
```

---

## 11. CLI ma'lumotnomasi

### Buyruqlar

| Buyruq | Tavsif |
|---|---|
| `qanot init [dir]` | Interaktiv sozlash sihrbozi. config.json va workspace yaratadi. |
| `qanot start [path]` | Botni OS xizmati (launchd/systemd) orqali ishga tushiradi. |
| `qanot start -f` | Oldingi rejimda ishga tushirish (Docker, systemd, debugging uchun). |
| `qanot stop [path]` | Botni to'xtatadi. |
| `qanot restart [path]` | Botni qayta ishga tushiradi (stop + start). |
| `qanot status [path]` | Bot ishlayotganligini tekshiradi. |
| `qanot logs [path]` | Bot loglarini ko'rsatadi (`-n50` qator soni uchun). |
| `qanot doctor [path]` | O'rnatishda salomatlik tekshiruvlarini bajaradi. |
| `qanot doctor --fix` | Aniqlangan muammolarni avtomatik tuzatadi. |
| `qanot backup [path]` | workspace/sessions/cron'ni `.tar.gz` ga eksport qiladi. |
| `qanot config show` | Joriy konfiguratsiyani ko'rsatadi. |
| `qanot config set <key> <value>` | Config qiymatini o'rnatadi. |
| `qanot config add-provider` | Zaxira AI provider qo'shadi (interaktiv). |
| `qanot config remove-provider` | AI provider'ni olib tashlaydi. |
| `qanot plugin new <name>` | Yangi plugin papkasi yaratadi. |
| `qanot plugin list` | O'rnatilgan plugin'larni ko'rsatadi. |
| `qanot update` | PyPI'dan eng yangi versiyaga yangilaydi + qayta ishga tushiradi. |
| `qanot version` | O'rnatilgan versiyani ko'rsatadi. |
| `qanot help` | Yordam ko'rsatadi. |

### Muhit o'zgaruvchilari

| O'zgaruvchi | Tavsif |
|---|---|
| `QANOT_CONFIG` | config.json ga yo'l (standart qidiruvni bekor qiladi). |

### Config yo'l aniqlash tartibi

CLI `config.json` ni shu tartibda qidiradi:

1. Pozitsion argument (fayl yoki papka)
2. `QANOT_CONFIG` muhit o'zgaruvchisi
3. `./config.json` (joriy papka)
4. `/data/config.json` (Docker standarti)

---

## 12. Muammolarni hal qilish

### Bot ishga tushmayapti

**Belgi:** `qanot start` hech narsa yoki xato ko'rsatmaydi.

```bash
# Holatni tekshirish
qanot status

# Loglarni tekshirish
qanot logs

# Xatolarni ko'rish uchun oldingi rejimda ishga tushirish
qanot start -f
```

**Keng tarqalgan sabablar:**

- Noto'g'ri `bot_token` -- @BotFather bilan tekshiring
- Noto'g'ri `api_key` -- provider dashboard'ni tekshiring
- `webhook_port` yoki `dashboard_port` da port ziddiyati

### "Config file not found"

```bash
# Config mavjudligini tekshirish
ls config.json

# To'g'ri joyni ko'rsatish
export QANOT_CONFIG=/path/to/config.json
qanot start
```

Yoki yangi config yaratish uchun `qanot init` ni ishga tushiring.

### API kalit noto'g'ri

```bash
# Salomatlik tekshiruvlarini ishga tushirish
qanot doctor

# Doctor bot token va API kalitlarni tekshiradi
```

Agar SecretRef ishlatayotgan bo'lsangiz:

```bash
# Muhit o'zgaruvchisi o'rnatilganligini tekshirish
echo $ANTHROPIC_API_KEY

# Fayl mavjud va o'qish mumkinligini tekshirish
cat /run/secrets/anthropic_key
```

### Kontekst to'lib ketishi / "Kontekst to'ldi"

Bot 60% kontekst ishlatilishida avtomatik compact qiladi. Agar tez-tez compaction ko'rsangiz:

```json
{
  "max_context_tokens": 200000,
  "history_limit": 30,
  "max_memory_injection_chars": 2000
}
```

- Modelingiz qo'llab-quvvatlasa `max_context_tokens` ni oshiring
- Qayta ishga tushirishda kamroq navbat tiklash uchun `history_limit` ni kamaytiring
- Kamroq RAG kontekst inject qilish uchun `max_memory_injection_chars` ni kamaytiring

### Ovoz ishlamayapti

```bash
qanot doctor
```

Doctor natijasidagi "Voice" bo'limini tekshiring. Keng tarqalgan muammolar:

- **API kalit yo'q:** `voice_api_keys.muxlisa` (yoki boshqa provider'ingiz) ni o'rnating
- **ffmpeg o'rnatilmagan:** KotibAI va Whisper uchun kerak (Muxlisa OGG ni to'g'ridan-to'g'ri qabul qiladi)
- **Noto'g'ri voice_mode:** `"inbound"` yoki `"always"` bo'lishi kerak, `"off"` emas

ffmpeg'ni o'rnatish:

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

### Bot sekin javob beryapti

1. **Javob rejimini tekshiring:** `"stream"` eng tez (Telegram Bot API 9.5 `sendMessageDraft` ishlatadi)
2. **Routing'ni yoqing:** Oddiy xabarlarni arzonroq/tezroq modellarga yo'naltiring
3. **Modelni tekshiring:** Haiku/Flash modellari Opus/Pro'dan tezroq javob beradi
4. **Bir vaqtdalik limitni tekshiring:** Ko'p foydalanuvchi bo'lsa `max_concurrent` ni oshiring

```json
{
  "response_mode": "stream",
  "routing_enabled": true,
  "max_concurrent": 8
}
```

### RAG xotiralarni topa olmayapti

```bash
# RAG yoqilganligini tekshirish
qanot config show

# Embedder initsializatsiyasi uchun loglarni tekshirish
qanot logs | grep -i "embedder\|rag"
```

Agar "FTS-only mode" ko'rsangiz, embedding provider topilmagan. Bepul vektor qidiruv uchun Gemini kalit qo'shing:

```json
{
  "providers": [
    {
      "name": "gemini-embed",
      "provider": "gemini",
      "model": "gemini-2.5-flash",
      "api_key": "AIza..."
    }
  ]
}
```

### Bot loop'da qolib ketyapti

Circuit breaker 3 ta bir xil ketma-ket tool chaqiruvdan keyin (`MAX_SAME_ACTION = 3`) botni to'xtatadi. Agar bot qotib qolgandek ko'rinsa:

- Tool'ning deterministik xatolar qaytarayotganligini tekshiring (agent doimiy xatolar uchun `_hint` inject qiladi)
- Har bir navbat uchun maksimal 25 iteratsiya (`MAX_ITERATIONS = 25`)
- Delegatsiya tool'lari uchun timeout 5 daqiqa (`LONG_TOOL_TIMEOUT = 300`)

### Eskirgan sessiyalar / yuqori disk ishlatilishi

```bash
# Doctor'ni avtomatik tuzatish bilan ishga tushirish
qanot doctor --fix

# Yoki qo'lda backup yaratish va tozalash
qanot backup
```

Doctor 30 kundan eski sessiyalarni avtomatik arxivlaydi va sessiya fayllari 100 MB dan oshganda ogohlantiradi.

### Plugin yuklanmayapti

```bash
# Plugin'larni va ularning holatini ko'rsatish
qanot plugin list

# Doctor natijasini tekshirish
qanot doctor
```

Plugin quyidagilarga mos kelishiga ishonch hosil qiling:
1. `plugins_dir` papkasida joylashgan
2. `QanotPlugin` class'ga ega `plugin.py` fayli bor
3. `config.json` dagi `plugins` massivida ro'yxatga olingan
4. `"enabled": true` o'rnatilgan

### Webhook rejimi ishlamayapti

```json
{
  "telegram_mode": "webhook",
  "webhook_url": "https://bot.example.com/webhook",
  "webhook_port": 8443
}
```

Talablar:
- `webhook_url` haqiqiy sertifikat bilan HTTPS bo'lishi kerak
- Port quyidagilardan biri bo'lishi kerak: 443, 80, 88 yoki 8443
- Server Telegram'ning IP diapazonlaridan yetib borish mumkin bo'lishi kerak

Ishonchingiz komil bo'lmasa, `"telegram_mode": "polling"` (standart) ishlating.
