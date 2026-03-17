# TOOLS.md - Tool Configuration & Notes

## MoySklad Integration (ms_*)

Siz MoySklad ombor boshqaruv tizimiga to'g'ridan-to'g'ri ulangansiz. 30 ta ms_* tool mavjud.

**MUHIM:** Login ma'lumotlari OLDINDAN sozlangan. Foydalanuvchidan HECH QACHON login, parol yoki token so'ramang. Tizimga kirish avtomatik amalga oshiriladi.

### Tovarlar:
- `ms_search_products` — Tovar qidirish (nomi, artikul).
- `ms_get_product` — Bitta tovar tafsilotlari (ID bo'yicha).
- `ms_get_assortment` — Yagona katalog (tovarlar + xizmatlar + variantlar).
- `ms_get_product_folders` — Tovar kategoriyalari.
- `ms_get_currencies` — Valyutalar.

### Ombor / Qoldiq:
- `ms_get_stock` — Tovarlar qoldig'i (barcha omborlarda).
- `ms_get_stock_by_store` — Ombor bo'yicha qoldiq.
- `ms_get_stores` — Omborlar ro'yxati.

### Kontragentlar:
- `ms_search_counterparties` — Mijoz/ta'minotchi qidirish.
- `ms_get_counterparty` — Kontragent tafsilotlari.
- `ms_counterparty_report` — Kontragent hisoboti (sotuvlar, qarz).

### Sotuvlar:
- `ms_get_customer_orders` — Buyurtmalar ro'yxati.
- `ms_get_customer_order` — Buyurtma tafsilotlari.
- `ms_get_demands` — Sotuvlar (jo'natmalar).
- `ms_get_sales_returns` — Qaytarilgan sotuvlar.
- `ms_sales_chart` — Sotuv grafigi (vaqt bo'yicha).

### Xaridlar:
- `ms_get_purchase_orders` — Xarid buyurtmalari.
- `ms_get_supplies` — Kirimlar.
- `ms_get_purchase_returns` — Qaytarilgan xaridlar.

### To'lovlar:
- `ms_get_payments_in` — Kiruvchi to'lovlar.
- `ms_get_payments_out` — Chiquvchi to'lovlar.
- `ms_get_invoices_out` — Chiquvchi fakturalar.
- `ms_get_invoices_in` — Kiruvchi fakturalar.

### Hisobotlar:
- `ms_profit_by_product` — Tovar rentabelligi.
- `ms_profit_by_counterparty` — Kontragent rentabelligi.
- `ms_turnover` — Tovar aylanmasi.
- `ms_cash_flow` — Pul oqimi grafigi.
- `ms_orders_chart` — Buyurtmalar grafigi.

### Tashkilot:
- `ms_get_organizations` — Yuridik shaxslar.
- `ms_get_employees` — Xodimlar.

### Ishlatish namunalari:
- "Omborda nima bor?" → `ms_get_stock`
- "Eng ko'p sotilgan tovar?" → `ms_profit_by_product`
- "Mijoz qarzi?" → `ms_counterparty_report`
- "Bugungi sotuvlar?" → `ms_sales_chart` (momentFrom/To bugun)
- "Tovar qidirish: telefon" → `ms_search_products` (search: telefon)

### Muhim:
- Pul summalari **tiyinda** (kopeykalarda) qaytadi — 100 ga bo'lib so'mga aylantiring
- Sanalar Moscow vaqt zonasida: `YYYY-MM-DD HH:mm:ss`
