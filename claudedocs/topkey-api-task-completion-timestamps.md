# TopKey API — Task completion timestamps kerak

**Maqsad:** TopKey REST API orqali har bir vazifa qachon "Bajarilgan" holatga o'tganini bilish.
**Hozirgi holat:** Imkonsiz. Ma'lumot bazada bor, lekin API javobida yo'q.
**Ta'sir:** Boshqaruvchilar "kim o'z vaqtida ishladi, kim kechikdi" hisobotini ololmaydi.

---

## Muammo

Qanot AI bot Asia Home jamoasining "kim qancha vazifani o'z vaqtida bajardi" deb so'roviga to'g'ri javob bera olmayapti. Sababi:

1. **`completed_on` maydoni ko'p hollarda `null`.** Xodimlar TopKey UI'da vazifani "Bajarilgan" holatiga o'tkazganda `completed_on` avtomatik to'lmasligi mumkin. 125 ta bajarilgan vazifani tekshirib, 55 tasida bu maydon bo'sh ekanligini topdik (live API probe bilan tasdiqlandi).
2. **Alternativa yo'q.** Vazifa qachon haqiqatda yopilganini boshqa hech qanday API javobidan topib bo'lmaydi:
   - `/api/v1/task/{id}` → `created_at` va `updated_at` har doim `null` qaytaradi.
   - `/api/v1/task/{id}/history` → board column o'tishlarini qaytaradi (qaysi ustunga ko'chgan, kim ko'chirgan), lekin **vaqt ko'rsatkichi yo'q**. JSON javobda faqat `id`, `user`, `sub_task`, `board_column` keys mavjud.
3. **Bazada ma'lumot bor.** SQL xato xabarlarida `task_history` jadvalida `created_at` va `updated_at` ustunlari mavjudligi ko'rinadi:
   ```
   SQLSTATE[42S22]: Column not found: 1054 Unknown column 'task_history.column_name' in 'field list'
   (Connection: mysql, SQL: select `task_history`.`id`, `task_history`.`task_id`,
    `task_history`.`user_id`, `task_history`.`board_column_id`, `task_history`.`sub_task_id`,
    `task_history`.`created_at`, `task_history`.`updated_at`, ...)
   ```
   Ya'ni jadvalda timestamp bor — Eloquent serializer uni hidden qilib qo'ygan.

---

## Talab (3 variantdan biri yetadi)

### Variant A — Eng oson, eng yaxshi (tavsiya qilinadi)

`task_history` modelida `created_at` ni `$hidden` ro'yxatidan olib tashlang. Shunda `GET /api/v1/task/{id}/history` javobida har bir transition uchun `created_at` keladi.

**Misol — hozirgi javob:**
```json
{
  "data": [
    {"id": 165, "user": null, "sub_task": null, "board_column": {"slug": "completed"}}
  ]
}
```

**Misol — kerakli javob:**
```json
{
  "data": [
    {
      "id": 165,
      "user": null,
      "sub_task": null,
      "board_column": {"slug": "completed"},
      "created_at": "2026-04-27T15:32:18+05:00"
    }
  ]
}
```

Shu o'zgarish bilan biz har vazifa "Bajarilgan" ustuniga qachon ko'chganini bilamiz — bu vazifa haqiqatda qachon yopilganligi.

### Variant B — `completed_on` ni avtomatik to'ldirish

Vazifa statusi `completed` bo'lganda yoki "Bajarilgan" board_column'ga ko'chirilganda — backend trigger / observer orqali `completed_on` ni `now()` qilib qo'yish.

Bu UX ham yaxshilaydi (xodim qo'lda sana kiritmaydi), lekin orqaga qarab tarixiy null'larni to'ldirmaydi. Faqat keyingi vazifalar uchun ishlaydi.

### Variant C — `task_history` uchun yangi endpoint

`GET /api/v1/task/{id}/transitions` (yoki shu kabi) — yangi serializer, har transition uchun timestamp bilan:

```json
{
  "data": [
    {
      "task_id": 27,
      "from_column_id": 23,
      "from_column_slug": "doing",
      "to_column_id": 24,
      "to_column_slug": "completed",
      "moved_by_user_id": 3,
      "moved_at": "2026-04-27T15:32:18+05:00"
    }
  ]
}
```

Eng to'liq variant, lekin yangi endpoint qo'shish kerak.

---

## Sinov (qabul qilish mezonlari)

API o'zgarishidan keyin quyidagi probe muvaffaqiyatli ishlashi kerak:

**1. History endpoint timestamp qaytaradi:**
```bash
curl -H "Authorization: Bearer <token>" \
  https://topkey.uz/api/v1/task/27/history
# Javobda har row uchun `created_at` (yoki `moved_at`) bo'lishi shart.
```

**2. Vaqt o'sib boruvchi tartibda:**
History rowlar `created_at` bo'yicha to'g'ri tartiblanadi (eng oxirgi transition — eng so'nggi yopilish).

**3. Aniq sana — taxminiy emas:**
Timestamp aniq vaqtni qaytaradi (`2026-04-27T15:32:18+05:00`), `2026-04-27T00:00:00` emas. Aks holda kunlik aniqlikda kechikishni o'lchab bo'lmaydi.

---

## Texnik kontekst (TopKey developer uchun)

**Identifikatsiya qilingan kod:** `app/Models/TaskHistory.php` (yoki shunga o'xshash) — `$hidden = ['created_at', 'updated_at']` mavjud bo'lsa kerak. Yoki `TaskHistoryResource` API resource transformer bularni ataylab tashlab yuboryapti.

**Probe natijalari:** Live API'ni 2026-05-02 sanasida tekshirdik. Test ma'lumotlari `claudedocs/topkey-probe-results.md` da (kerak bo'lsa).

**Affected endpoints:**
- `GET /api/v1/task/{id}/history` — asosiy (Variant A uchun)
- `PUT /api/v1/task/{id}` (status update) — Variant B uchun trigger nuqtasi

**Mavjud field projection:** `?fields=id,task_id,created_at,updated_at` ham urinib ko'rdik — Eloquent select to'g'ri ishlaydi (SQL'ga columnlar qo'shiladi), lekin JSON serializer ularni JSON'dan tashlab yuboryapti. Demak muammo `$hidden` yoki `toArray()` darajasida.

---

## Kim hal qiladi

Qanot AI tomonida bu **hal qilib bo'lmaydi** — TopKey'ning serializer / model konfiguratsiyasiga kirish kerak. Bu task TopKey dasturchilariga (yoki vendoriga) yuborilishi kerak.

Qanot AI tomonida hozircha qilingan kompromis: `completion_breakdown` 3 ta bucketga ajratilgan (`O'z vaqtida` / `Kechikkan` / `Sana noma'lum`) — `unknown_date` `completed_on=null` bo'lgan vazifalar. Bu hisobotlarda haqiqatni yashirmasdan ko'rsatadi. TopKey API tuzatilgandan keyin esa `Sana noma'lum` bucketi yo'qoladi (yoki keskin kamayadi) va biz aniq on-time/late hisobotini berishimiz mumkin bo'ladi.
