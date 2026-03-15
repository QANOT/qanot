# TOOLS.md - Tool Configuration & Notes

## amoCRM CRM Integration (amocrm_*)

Siz amoCRM CRM tizimiga to'g'ridan-to'g'ri ulangansiz. 34 ta amocrm_* tool mavjud.

**MUHIM:** Login ma'lumotlari OLDINDAN sozlangan. Foydalanuvchidan HECH QACHON token, parol yoki API URL so'ramang. Tizimga kirish avtomatik amalga oshiriladi.

### Lidlar (Sdelkalar):
- `amocrm_get_leads` — Lidlar ro'yxati. query, pipeline_id, status_id bo'yicha filter.
- `amocrm_get_lead` — Bitta lid tafsilotlari (ID bo'yicha).
- `amocrm_create_lead` — Yangi lid yaratish (nomi, narxi, pipeline, status).
- `amocrm_update_lead` — Lid yangilash (status, narx, custom fieldlar).
- `amocrm_get_leads_summary` — Barcha lidlar umumiy hisoboti (jami soni, jami summa, pipeline/status bo'yicha).

### Kontaktlar:
- `amocrm_get_contacts` — Kontaktlar ro'yxati yoki qidirish.
- `amocrm_get_contact` — Bitta kontakt ma'lumotlari (ID bo'yicha).
- `amocrm_create_contact` — Yangi kontakt yaratish (ism, telefon, email).

### Kompaniyalar:
- `amocrm_get_companies` — Kompaniyalar ro'yxati yoki qidirish.
- `amocrm_get_company` — Bitta kompaniya tafsilotlari (ID bo'yicha).
- `amocrm_create_company` — Yangi kompaniya yaratish (nomi, custom fieldlar).

### Voronkalar (Pipeline):
- `amocrm_get_pipelines` — Barcha voronkalar va ularning bosqichlari (statuslari).

### Vazifalar:
- `amocrm_get_tasks` — Vazifalar ro'yxati (mas'ul, bajarilgan/bajarilmagan bo'yicha filter).
- `amocrm_create_task` — Yangi vazifa yaratish (matn, muddat, bog'langan entity).

### Izohlar:
- `amocrm_add_note` — Lid yoki kontaktga izoh qo'shish.
- `amocrm_get_notes` — Entity izohlari ro'yxati (lid yoki kontakt, limit bilan).

### Teglar:
- `amocrm_get_tags` — Teglar ro'yxati (lidlar, kontaktlar yoki kompaniyalar uchun).
- `amocrm_add_tags` — Entity ga teg qo'shish (lid, kontakt yoki kompaniya).

### Kiruvchi murojaatlar (Unsorted):
- `amocrm_get_incoming_leads` — Kiruvchi (unsorted) murojaatlar ro'yxati.
- `amocrm_accept_incoming_lead` — Kiruvchi murojaatni qabul qilish (UID bo'yicha).
- `amocrm_get_incoming_summary` — Kiruvchi murojaatlar umumiy statistikasi.

### Bog'lanishlar (Links):
- `amocrm_get_links` — Entity bilan bog'langan elementlar ro'yxati.
- `amocrm_link_entities` — Ikki entity ni bir-biriga bog'lash (lid-kontakt, lid-kompaniya).

### Custom fieldlar:
- `amocrm_get_custom_fields` — Custom fieldlar ro'yxati (lid, kontakt yoki kompaniya uchun).

### Complex lead:
- `amocrm_create_complex_lead` — Lid va kontaktni birga yaratish (telefon, email bilan).

### Foydalanuvchilar:
- `amocrm_get_users` — CRM foydalanuvchilari (menejerlar) ro'yxati.

### Hodisalar:
- `amocrm_get_events` — So'nggi hodisalar ro'yxati (turi bo'yicha filter).

### Chatlar (Yozishmalar):
- `amocrm_get_talks` — Chatlar ro'yxati (o'qilgan/o'qilmagan, holat bo'yicha filter).
- `amocrm_get_talk` — Bitta chat tafsilotlari (ID bo'yicha).
- `amocrm_get_chat_messages` — Xabarlar tarixi — kim qachon yozgani. Lid yoki kontakt bo'yicha.
- `amocrm_get_unread_chats` — O'qilmagan chatlar — javob kutayotgan mijozlar.

### Manbalar:
- `amocrm_get_sources` — Lid manbalari ro'yxati (reklama kanallari).

### Akkaunt:
- `amocrm_get_account` — amoCRM akkaunt ma'lumotlari (amojo_id, users_groups va h.k.).

### Webhooklar:
- `amocrm_get_webhooks` — Ro'yxatga olingan webhooklar.

### Foydalanish misollari:

**Lidlar bilan ishlash:**
```
# Barcha lidlarni ko'rish
amocrm_get_leads()

# Ma'lum voronkadagi lidlar
amocrm_get_leads(pipeline_id=12345)

# Lid yaratish
amocrm_create_lead(name="Yangi buyurtma", price=5000000, pipeline_id=12345, status_id=67890)

# Lid statusini yangilash
amocrm_update_lead(lead_id=111, status_id=222)

# Lid + kontakt birga yaratish
amocrm_create_complex_lead(lead_name="Buyurtma", contact_name="Alisher", phone="+998901234567", price=3000000)
```

**Kontaktlar:**
```
# Kontakt qidirish
amocrm_get_contacts(query="Alisher")

# Kontakt yaratish telefon bilan
amocrm_create_contact(name="Alisher Karimov", phone="+998901234567")
```

**Kompaniyalar:**
```
# Kompaniyalarni qidirish
amocrm_get_companies(query="Tech")

# Kompaniya yaratish
amocrm_create_company(name="Qanot Tech LLC")
```

**Vazifalar:**
```
# Bajarilmagan vazifalar
amocrm_get_tasks(is_completed=0)

# Vazifa yaratish
amocrm_create_task(text="Mijozga qo'ng'iroq qilish", complete_till=1710000000, entity_id=111, entity_type="leads")
```

**Teglar:**
```
# Lidlar teglari
amocrm_get_tags(entity_type="leads")

# Lidga teg qo'shish
amocrm_add_tags(entity_id=111, entity_type="leads", tags=[{"name": "VIP"}])
```

**Bog'lanishlar:**
```
# Lid bilan bog'langan entitylar
amocrm_get_links(entity_id=111, entity_type="leads")

# Lidni kontaktga bog'lash
amocrm_link_entities(entity_id=111, entity_type="leads", to_entity_id=222, to_entity_type="contacts")
```

**Chatlar:**
```
# O'qilmagan chatlarni ko'rish
amocrm_get_unread_chats()

# Lid bo'yicha xabarlar tarixi
amocrm_get_chat_messages(entity_id=33538767, entity_type="leads")

# Barcha chatlar
amocrm_get_talks(limit=20)
```

**Kiruvchi murojaatlar:**
```
# Kiruvchi murojaatlar
amocrm_get_incoming_leads()

# Murojaatni qabul qilish
amocrm_accept_incoming_lead(uid="abc-123-def")

# Kiruvchi murojaatlar statistikasi
amocrm_get_incoming_summary()
```

### Qoidalar:
- **MUHIM: Foydalanuvchiga API, token, endpoint, JSON kabi texnik so'zlarni HECH QACHON aytmang. Shunchaki natijani taqdim eting.**
- Login ma'lumotlarini so'ramang — avtomatik.
- Lid yaratganda avval `amocrm_get_pipelines` bilan voronkalarni tekshiring.
- Kontakt yaratganda telefon raqamni +998 formatida kiriting.
- Muhim o'zgarishlardan keyin `amocrm_add_note` bilan izoh qoldiring.
- Custom fieldlarni olish uchun `amocrm_get_custom_fields` dan foydalaning.
- Lid va kontaktni birga yaratish uchun `amocrm_create_complex_lead` qulay.
