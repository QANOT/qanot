# TOOLS.md - Tool Configuration & Notes

> Document tool-specific configurations, gotchas, and credentials here.

---

## Built-in Tools

**Status:** All working

### File Operations
- `read_file` — Read a file from workspace
- `write_file` — Write/create a file
- `list_files` — List directory contents

### System
- `run_command` — Run sandboxed shell commands (python3, curl, ffmpeg, zip, git, pip)
- `web_search` — Search the web via DuckDuckGo
- `memory_search` — Search across memory files

### O'zbekiston Biznes Toollar
- `currency_rate` — CBU rasmiy valyuta kurslari (USD, EUR, RUB...)
- `ikpu_search` — IKPU (MXIK) tovar klassifikator kodini qidirish
- `payment_link` — Click/Payme to'lov havolasi yaratish
- `tax_calculator` — QQS, aylanma soliq, ustama, nasiya kalkulyatori
- `generate_document` — Rasmiy biznes hujjat yaratish (20 tur):
  - **Shartnomalar:** shartnoma, oldi_sotdi (FK 386), yetkazib_berish (FK 437), ijara (FK 535), mehnat (MK 103), pudrat (FK 631), xizmat (FK 703), nda (O'RQ-370)
  - **Hujjatlar:** faktura, dalolatnoma, qabul_topshirish, solishtirma, ishonchnoma, talabnoma, tijorat_taklifi (FK 365)
  - **HR:** buyruq_t1 (ishga qabul, VMQ 1297), buyruq_t6 (ta'til), buyruq_t8 (bo'shatish), ariza (3 xil), tushuntirish_xati
- `weather` — Ob-havo ma'lumoti

### Session
- `session_status` — Check context usage, token count

### Scheduling
- `cron_create` — Create a scheduled job
- `cron_list` — List all scheduled jobs
- `cron_update` — Update a scheduled job
- `cron_delete` — Delete a scheduled job

---

## What Goes Here

- Tool configurations and settings
- Credential locations (not the credentials themselves!)
- Gotchas and workarounds discovered
- Common commands and patterns
- Integration notes

---

*Add whatever helps you do your job. This is your cheat sheet.*
