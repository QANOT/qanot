# TOOLS.md - Tool Configuration & Notes

## Bitrix24 CRM Integration (bitrix24_*)

Siz Bitrix24 CRM tizimiga to'g'ridan-to'g'ri ulangansiz. 20 ta bitrix24_* tool mavjud.

**MUHIM:** Login ma'lumotlari OLDINDAN sozlangan. Foydalanuvchidan HECH QACHON webhook, parol yoki API URL so'ramang. Tizimga kirish avtomatik amalga oshiriladi.

### Sdelkalar (Deals):
- `bitrix24_get_deals` — Sdelkalar ro'yxati. Bosqich, voronka, mas'ul shaxs bo'yicha filter.
- `bitrix24_get_deal` — Bitta sdelka tafsilotlari (ID bo'yicha).
- `bitrix24_create_deal` — Yangi sdelka yaratish (nomi, summa, bosqich, voronka).
- `bitrix24_update_deal` — Sdelka yangilash (bosqich, summa, mas'ul shaxs).
- `bitrix24_get_deals_summary` — Barcha sdelkalar umumiy hisoboti (jami soni, jami summa, bosqich bo'yicha).

### Lidlar (Leads):
- `bitrix24_get_leads` — Lidlar ro'yxati. Status, manba, mas'ul shaxs bo'yicha filter.
- `bitrix24_get_lead` — Bitta lid tafsilotlari (ID bo'yicha).
- `bitrix24_create_lead` — Yangi lid yaratish (nomi, ism, telefon, manba).
- `bitrix24_update_lead` — Lid yangilash (status, summa, mas'ul shaxs).

### Kontaktlar:
- `bitrix24_get_contacts` — Kontaktlar ro'yxati yoki qidirish.
- `bitrix24_get_contact` — Bitta kontakt ma'lumotlari (ID bo'yicha).
- `bitrix24_create_contact` — Yangi kontakt yaratish (ism, familiya, telefon, email).

### Kompaniyalar:
- `bitrix24_get_companies` — Kompaniyalar ro'yxati.
- `bitrix24_get_company` — Bitta kompaniya ma'lumotlari (ID bo'yicha).

### Vazifalar (Tasks):
- `bitrix24_get_tasks` — Vazifalar ro'yxati (mas'ul shaxs, holat bo'yicha filter).
- `bitrix24_create_task` — Yangi vazifa yaratish (nomi, tavsif, muddat, mas'ul).

### Faoliyatlar (Activities):
- `bitrix24_get_activities` — CRM faoliyatlar (dela) ro'yxati.
- `bitrix24_create_activity` — Yangi faoliyat yaratish (uchrashuv, qo'ng'iroq, xat).

### Bosqichlar (Deal Stages):
- `bitrix24_get_deal_stages` — Sdelka bosqichlari (voronka statuslari) ro'yxati.

### Foydalanuvchilar:
- `bitrix24_get_users` — CRM foydalanuvchilari (menejerlar) ro'yxati.

### Foydalanish misollari:

**Sdelkalar bilan ishlash:**
```
# Barcha sdelkalarni ko'rish
bitrix24_get_deals()

# Ma'lum bosqichdagi sdelkalar
bitrix24_get_deals(STAGE_ID="NEW")

# Sdelka yaratish
bitrix24_create_deal(TITLE="Yangi buyurtma", OPPORTUNITY=5000000, STAGE_ID="NEW", CURRENCY_ID="UZS")

# Sdelka bosqichini yangilash
bitrix24_update_deal(deal_id=111, STAGE_ID="WON")

# Umumiy hisobot
bitrix24_get_deals_summary()
```

**Lidlar bilan ishlash:**
```
# Barcha lidlar
bitrix24_get_leads()

# Lid yaratish telefon bilan
bitrix24_create_lead(TITLE="Yangi lid", NAME="Alisher", PHONE="+998901234567")

# Lid statusini yangilash
bitrix24_update_lead(lead_id=55, STATUS_ID="IN_PROCESS")
```

**Kontaktlar:**
```
# Kontakt qidirish
bitrix24_get_contacts(NAME="Alisher")

# Kontakt yaratish
bitrix24_create_contact(NAME="Alisher", LAST_NAME="Karimov", PHONE="+998901234567", EMAIL="alisher@mail.uz")
```

**Vazifalar:**
```
# Bajarilmagan vazifalar
bitrix24_get_tasks(STATUS=3)

# Vazifa yaratish
bitrix24_create_task(TITLE="Mijozga qo'ng'iroq qilish", DEADLINE="2025-12-31 18:00:00", RESPONSIBLE_ID=1)
```

**Faoliyatlar:**
```
# Sdelkaga bog'langan faoliyatlar
bitrix24_get_activities(OWNER_TYPE_ID=2, OWNER_ID=111)

# Yangi qo'ng'iroq rejalashtirish
bitrix24_create_activity(SUBJECT="Mijozga qo'ng'iroq", TYPE_ID=2, OWNER_TYPE_ID=2, OWNER_ID=111)
```

**Bosqichlar va foydalanuvchilar:**
```
# Voronka bosqichlarini ko'rish
bitrix24_get_deal_stages()

# CRM menejerlari
bitrix24_get_users(ACTIVE=true)
```

### Qoidalar:
- **MUHIM: Foydalanuvchiga API, webhook, endpoint, JSON kabi texnik so'zlarni HECH QACHON aytmang. Shunchaki natijani taqdim eting.**
- Login ma'lumotlarini so'ramang — avtomatik.
- Sdelka yaratganda avval `bitrix24_get_deal_stages` bilan bosqichlarni tekshiring.
- Kontakt yaratganda telefon raqamni +998 formatida kiriting.
- Muhim o'zgarishlardan keyin `bitrix24_create_activity` bilan faoliyat yarating.
