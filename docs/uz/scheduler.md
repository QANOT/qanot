# Cron Scheduler

Qanot AI rejali vazifalarni bajarish uchun APScheduler asosidagi cron tizimini o'z ichiga oladi. Agent tabiiy suhbat orqali cron job'larni yaratishi, yangilashi va o'chirishi mumkin.

## Cron job'lar qanday ishlaydi

Cron job'lar `{cron_dir}/jobs.json` da aniqlanadi. Har bir job'da nom, cron jadval, bajarish rejimi va agent'ga nima qilish kerakligini aytadigan prompt bor.

```json
[
  {
    "name": "daily-summary",
    "schedule": "0 20 * * *",
    "mode": "isolated",
    "prompt": "Summarize today's conversations and update MEMORY.md",
    "enabled": true
  }
]
```

### Cron ifoda formati

Standart 5 maydonli cron: `daqiqa soat kun oy hafta_kuni`

| Ifoda | Ma'nosi |
|-------|---------|
| `0 */4 * * *` | Har 4 soatda |
| `0 20 * * *` | Har kuni 20:00 da |
| `30 9 * * 1-5` | Ish kunlari 9:30 da |
| `0 0 1 * *` | Har oyning birinchi kuni |
| `*/15 * * * *` | Har 15 daqiqada |

Config'dagi timezone jadvallash uchun ishlatiladi (standart: `Asia/Tashkent`).

## Bajarish rejimlari

### isolated

Mustaqil agent yaratadi — o'zining suhbat tarixi, context tracker'i va session writer'i bilan.

```json
{
  "mode": "isolated",
  "prompt": "Check for overdue tasks in the workspace"
}
```

**Qanday ishlaydi:**

1. Yangi `Agent` instansiyasi `prompt_mode="minimal"` bilan yaratiladi (faqat SOUL.md + TOOLS.md + sessiya ma'lumotlari)
2. Prompt user xabar sifatida yuboriladi
3. Agent to'liq tool loop'ini ishga tushiradi (25 ta iteratsiyagacha)
4. Natijalar alohida sessiya fayliga loglanadi (`cron-{name}-{timestamp}.jsonl`)
5. Agar agent `proactive-outbox.md` ga yozsa, kontent barcha ruxsat etilgan foydalanuvchilarga yuboriladi

**Qachon ishlatish kerak:** Davom etayotgan foydalanuvchi suhbatlariga ta'sir qilmasligi kerak bo'lgan fon vazifalari. Masalan: xotira tozalash, vaqti-vaqti bilan veb tekshirish, ma'lumotlarni qayta ishlash.

### systemEvent

Prompt'ni asosiy agent'ning xabar navbatiga tizim hodisasi sifatida inject qiladi.

```json
{
  "mode": "systemEvent",
  "prompt": "Remind the user about their 3pm meeting"
}
```

**Qanday ishlaydi:**

1. Prompt scheduler'ning xabar navbatiga qo'yiladi
2. Telegram adapter'ining proaktiv loop'i uni oladi
3. Asosiy agent uni oddiy navbat sifatida qayta ishlaydi (to'liq suhbat konteksti bilan)

**Qachon ishlatish kerak:** Joriy suhbat konteksti kerak bo'lgan yoki davom etayotgan suhbat qismi sifatida ko'rinishi kerak bo'lgan vazifalar. Masalan: eslatmalar, vaqtga asoslangan kuzatuvlar, rejali tekshiruvlar.

## Standart heartbeat job

Agar `jobs.json` da mavjud bo'lmasa, heartbeat job avtomatik yaratiladi:

```json
{
  "name": "heartbeat",
  "schedule": "0 */4 * * *",
  "mode": "isolated",
  "prompt": "HEARTBEAT: Read HEARTBEAT.md and perform self-improvement checks:\n1. Check proactive-tracker.md -- overdue behaviors?\n2. Pattern check -- repeated requests to automate?\n3. Outcome check -- decisions >7 days old to follow up?\n4. Memory -- context %, update MEMORY.md with distilled learnings\n5. Proactive surprise -- anything to delight human?\nIf you have a message for the human, write it to /data/workspace/proactive-outbox.md",
  "enabled": true
}
```

Bu har 4 soatda ishlaydi, agent'ni o'z holatini ko'rib chiqish, xotirani tozalash va ixtiyoriy ravishda foydalanuvchiga proaktiv xabar yuborishga undaydi.

## Proaktiv xabar yuborish

Cron job'lar `proactive-outbox.md` mexanizmi orqali foydalanuvchilarga xabar yuborishi mumkin:

1. Isolated cron job `{workspace_dir}/proactive-outbox.md` ga kontent yozadi
2. Job tugagandan keyin scheduler outbox'ni tekshiradi
3. Agar kontent bo'lsa, barcha `allowed_users` ga Telegram orqali yuboriladi
4. Outbox tozalanadi

Bu isolated cron job'larning foydalanuvchilar bilan aloqa qilishning yagona yo'li. System event job'lar to'g'ridan-to'g'ri suhbat orqali aloqa qiladi.

## Cron job'larni boshqarish

### Tool'lar orqali (suhbatda)

Agent tabiiy suhbat orqali cron job'larni boshqara oladi:

```
User: Har kuni kechki 8 da xulosa qil
Agent: [cron_create ni chaqiradi: name="daily-summary", schedule="0 20 * * *", ...]
```

Mavjud tool'lar: `cron_create`, `cron_list`, `cron_update`, `cron_delete`. Parametrlar uchun [Tools](tools.md) ga qarang.

### cron_create parametrlari

| Parametr | Tur | Majburiy | Tavsif |
|----------|-----|----------|--------|
| `name` | string | Ha | Noyob job nomi |
| `prompt` | string | Ha | Eslatma matni yoki vazifa prompt'i |
| `schedule` | string | Yo'q* | Cron ifoda (masalan, `0 9 * * *`) takroriy job'lar uchun |
| `at` | string | Yo'q* | ISO 8601 vaqt belgisi (masalan, `2026-03-12T17:00:00+05:00`) bir martalik eslatmalar uchun |
| `mode` | string | Yo'q | `"systemEvent"` (matn yetkazish) yoki `"isolated"` (to'liq agent). Standart: `"systemEvent"` |
| `delete_after_run` | boolean | Yo'q | Bajarilgandan keyin job'ni avtomatik o'chirish. Standart: `at` eslatmalar uchun `true`, takroriy uchun `false` |
| `timezone` | string | Yo'q | Bu job uchun IANA timezone almashtirish (masalan, `"Asia/Tashkent"`, `"Europe/London"`) |

\* `schedule` yoki `at` dan biri berilishi kerak. Takroriy job'lar uchun `schedule`, bir martalik eslatmalar uchun `at` ishlating.

**Bir martalik eslatmalar:** `at` parametrini ishlatganda, job ko'rsatilgan ISO 8601 vaqt belgisida bir marta ishlaydi va keyin avtomatik o'chiriladi (`delete_after_run` majburiy `true` qilinadi).

**Job bo'yicha timezone:** Standart holda job'lar config'dagi global timezone'ni ishlatadi. `timezone` parametri muayyan job uchun buni almashtiradi — foydalanuvchi boshqa vaqt zonasida eslatma kerak bo'lganda foydali.

### jobs.json orqali (qo'lda)

`{cron_dir}/jobs.json` ni to'g'ridan-to'g'ri tahrirlang. O'zgarishlar botni qayta ishga tushirgandan keyin kuchga kiradi, yoki agent qayta yuklashni boshlash uchun `cron_update` ni chaqira oladi.

## Job qayta yuklash

Cron tool `jobs.json` ni o'zgartirganda, `scheduler.reload_jobs()` ni chaqiradi:

1. APScheduler'dan barcha mavjud `cron_*` job'larni olib tashlaydi
2. `jobs.json` ni diskdan qayta o'qiydi
3. Heartbeat job mavjudligini ta'minlaydi
4. Barcha yoqilgan job'larni qayta qo'shadi

Bu o'zgarishlar botni qayta ishga tushirmasdan darhol kuchga kirishini ta'minlaydi.

## Xato boshqaruvi

- **Muvaffaqiyatsiz isolated job'lar:** Xatolar loglanadi, lekin asosiy botga ta'sir qilmaydi
- **Muvaffaqiyatsiz system event'lar:** Xatolar loglanadi va keyingi proaktiv loop iteratsiyasida qayta uriniladi
- **Noto'g'ri cron ifodalar:** 5 ta maydon bo'lmagan ifodali job'lar ogohlantirish bilan o'tkazib yuboriladi

## Arxitektura eslatmalari

- Scheduler APScheduler 3.x dan `AsyncIOScheduler` ishlatadi
- Har bir isolated job tizim prompt'larini kichik saqlash uchun `prompt_mode="minimal"` bilan yangi `Agent` oladi
- Scheduler asosiy agent bilan bir xil `LLMProvider` va `ToolRegistry` ni baham ko'radi
- Xabar navbati scheduler va Telegram adapter o'rtasidagi ko'prik bo'lgan `asyncio.Queue`
