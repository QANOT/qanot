# TOOLS.md - Tool Configuration & Notes

## 1C Enterprise Integration (onec_*)

Siz 1C Enterprise buxgalteriya tizimiga to'g'ridan-to'g'ri ulangansiz. 18 ta onec_* tool mavjud.

**MUHIM:** Login ma'lumotlari OLDINDAN sozlangan. Foydalanuvchidan HECH QACHON parol, URL yoki boshqa texnik ma'lumot so'ramang. Tizimga kirish avtomatik amalga oshiriladi.

### Kontragentlar (Hamkorlar):
- `onec_get_contractors` — Kontragentlar ro'yxati. Nomi bo'yicha qidirish mumkin.
- `onec_get_contractor` — Bitta kontragent tafsilotlari (ID bo'yicha).
- `onec_create_contractor` — Yangi kontragent yaratish (nomi, INN, to'liq nomi).

### Tovarlar (Nomenklatura):
- `onec_get_products` — Tovarlar ro'yxati. Nomi yoki guruh bo'yicha qidirish.
- `onec_get_product` — Bitta tovar tafsilotlari (ID bo'yicha).

### Sotuvlar:
- `onec_get_sales` — Sotuvlar hujjatlari ro'yxati. Sana va kontragent bo'yicha filter.
- `onec_get_sale` — Bitta sotuv hujjati tafsilotlari (ID bo'yicha).
- `onec_get_sales_summary` — Sotuvlar umumiy hisoboti — jami soni va summasi, kontragent bo'yicha taqsimot.

### Xaridlar:
- `onec_get_purchases` — Xaridlar hujjatlari ro'yxati. Sana va kontragent bo'yicha filter.
- `onec_get_purchase` — Bitta xarid hujjati tafsilotlari (ID bo'yicha).

### Kassa:
- `onec_get_cash_receipts` — Kassa kirim orderlari (pul kirimi). Sana bo'yicha filter.
- `onec_get_cash_expenses` — Kassa chiqim orderlari (pul chiqimi). Sana bo'yicha filter.

### Qoldiqlar:
- `onec_get_contractor_balance` — Kontragent bilan o'zaro hisob-kitob qoldig'i.

### Tashkilotlar:
- `onec_get_organizations` — Tashkilotlar ro'yxati.

### Omborlar:
- `onec_get_warehouses` — Omborlar ro'yxati.

### Valyuta kurslari:
- `onec_get_exchange_rates` — Valyuta kurslari. Sana va valyuta bo'yicha filter.

### Umumiy:
- `onec_get_metadata` — 1C bazasidagi barcha mavjud ob'ektlar ro'yxati.
- `onec_query` — Ixtiyoriy 1C ob'ektga so'rov (har qanday resurs va filter bilan).

### Foydalanish misollari:

**Kontragentlar bilan ishlash:**
```
# Barcha kontragentlarni ko'rish
onec_get_contractors()

# Kontragent qidirish
onec_get_contractors(search="Alisher")

# Yangi kontragent yaratish
onec_create_contractor(name="Alisher Savdo LLC", inn="123456789")
```

**Sotuvlar:**
```
# Bugungi sotuvlar
onec_get_sales(date_from="2026-03-16", date_to="2026-03-16")

# Mart oyidagi sotuvlar hisoboti
onec_get_sales_summary(date_from="2026-03-01", date_to="2026-03-31")

# Ma'lum kontragentning sotuvlari
onec_get_sales(contractor_key="xxx-xxx-xxx")
```

**Xaridlar:**
```
# Oxirgi xaridlar
onec_get_purchases(date_from="2026-03-01", date_to="2026-03-15")
```

**Kassa:**
```
# Bugungi kirimlar
onec_get_cash_receipts(date_from="2026-03-16", date_to="2026-03-16")

# Bugungi chiqimlar
onec_get_cash_expenses(date_from="2026-03-16", date_to="2026-03-16")
```

**Tovarlar:**
```
# Tovar qidirish
onec_get_products(search="telefon")

# Barcha tovarlar (100 ta)
onec_get_products(top=100)
```

**Valyuta kurslari:**
```
# Bugungi kurslar
onec_get_exchange_rates(date="2026-03-16")
```

**Ixtiyoriy so'rov:**
```
# Istalgan ob'ektga so'rov
onec_query(resource="Catalog_Валюты")

# Filter bilan
onec_query(resource="Document_СчетНаОплату", filter="Date ge datetime'2026-03-01T00:00:00'", top=20)
```

### Qoidalar:
- **MUHIM: Foydalanuvchiga OData, REST, API, GUID, JSON kabi texnik so'zlarni HECH QACHON aytmang. Shunchaki natijani taqdim eting.**
- Login ma'lumotlarini so'ramang — avtomatik.
- Pul summalarini formatlang: 1,500,000 so'm.
- Sanalarni tushunarli formatda ko'rsating: 15-mart 2026.
- Kontragent yaratganda INN ni to'g'ri kiriting.
- Sotuvlar hisoboti uchun avval `onec_get_sales_summary` dan foydalaning.
- Qaysi ob'ektlar mavjudligini bilish uchun `onec_get_metadata` dan foydalaning.
