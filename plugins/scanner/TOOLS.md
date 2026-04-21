# Scanner Tools

3 ta tool + 9 hujjat turini o'rgatadigan SOUL.

## `scanner_doctypes`
Qo'llab-quvvatlanadigan hujjat turlari ro'yxatini qaytaradi: har biri uchun kalit so'z, o'zbekcha nomlar, ajratib olinadigan maydonlar, default chiqish formati, va maxsus holatlar bo'yicha izohlar.

```json
{}
```

## `expense_categorize`
Do'kon nomini 14 ta kategoriyadan biriga tegishli deb topadi. Tez (regex-based), Uzbek kompaniyalarga moslashgan. Topa olmasa `null` qaytaradi — bu holda siz kontekstdan o'zingiz tanlaysiz yoki foydalanuvchidan so'raysiz.

```json
{
  "vendor": "Korzinka",
  "items": ["non", "sut"]
}
```

Valid kategoriyalar: Oziq-ovqat, Restoran, Transport, Yoqilg'i, Kommunal, Ijara, Maosh, Tovar, Tibbiyot, Ta'lim, Reklama, Ofis, Texnika, Boshqa.

## `expense_summary`
Berilgan davr uchun xarajatlar hisoboti. Siz avval `sheets_read` bilan "Xarajatlar 2026" jadvalni to'liq o'qib, uni `rows` sifatida yuborasiz. Tool toza agregatsiya qiladi — API chaqirmaydi.

```json
{
  "period": "month",
  "rows": [["Sana", "Do'kon", "Summa", ...], ["2026-04-21", "Korzinka", 156000, "UZS", "Oziq-ovqat", "", ""], ...]
}
```

Qabul qilinadigan davrlar:
- `today`, `yesterday`, `week`, `month`, `year`
- Aniq diapazon: `YYYY-MM-DD..YYYY-MM-DD`

Qaytaradi:
- `total_by_currency`: UZS/USD alohida jami
- `by_category`: har kategoriya bo'yicha
- `top_transactions`: eng katta 5 ta xarid
- `prev_total_by_currency`: oldingi davr (delta uchun) — faqat `prev_rows` berilgan bo'lsa
- `markdown`: tayyor Uzbek hisoboti

## Chek asosiy pattern (SOUL-dan qisqa)

Foydalanuvchi rasm yuborsa:
1. `scanner_doctypes` (noaniqlik bo'lsa) → hujjat turini aniqlang
2. Rasm ko'rib maydonlarni ajrating
3. `expense_categorize` (chek bo'lsa) → kategoriya toping
4. Natijani ko'rsatib, tasdiqlatib oling
5. `sheets_append` / `create_pdf` / `create_docx` / CRM tool bilan saqlang

Batafsil — SOUL_APPEND.md dagi "Hujjat turlari bo'yicha xususiyatlar" bo'limiga qarang.
