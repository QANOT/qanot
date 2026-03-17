# TOOLS.md - Tool Configuration & Notes

## Eskiz SMS Gateway Integration (eskiz_*)

Siz Eskiz SMS gateway ga to'g'ridan-to'g'ri ulangansiz. 13 ta eskiz_* tool mavjud.

**MUHIM:** Login ma'lumotlari OLDINDAN sozlangan. Foydalanuvchidan HECH QACHON email, parol yoki token so'ramang. Tizimga kirish avtomatik amalga oshiriladi.

### SMS yuborish:
- `eskiz_send_sms` — Bitta SMS yuborish. Telefon raqam (998901234567) va matn.
- `eskiz_send_batch` — Bir nechta SMS yuborish (ommaviy).
- `eskiz_check_message` — Xabar matnini tekshirish (belgilar, uzunlik).

### Holat tekshirish:
- `eskiz_get_sms_status` — Bitta SMS yetkazish holati (ID bo'yicha).
- `eskiz_get_dispatch_status` — Kampaniya statistikasi.

### Tarix:
- `eskiz_get_messages` — Yuborilgan xabarlar tarixi. Sana va holat bo'yicha filter.

### Akkaunt:
- `eskiz_get_balance` — Qolgan SMS krediti.
- `eskiz_get_user_info` — Akkaunt ma'lumotlari.
- `eskiz_get_nicknames` — Yuboruvchi nomlari (alpha-name) ro'yxati.

### Shablonlar:
- `eskiz_get_templates` — SMS shablonlar va ularning holati.

### Hisobotlar:
- `eskiz_get_totals` — Umumiy statistika (yuborilgan, sarflangan).
- `eskiz_get_monthly_report` — Oylik xarajatlar.
- `eskiz_get_range_report` — Sana oralig'idagi xarajatlar.

### Ishlatish namunalari:
- "Mijozga SMS yubor" → `eskiz_send_sms` (phone, message)
- "Balans qancha?" → `eskiz_get_balance`
- "Bugun nechta SMS yuborildi?" → `eskiz_get_totals`
- "SMS yetdimi?" → `eskiz_get_sms_status` (id)
