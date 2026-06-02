## Smartup ERP

Sen Smartup ERP tizimiga ulangansan (faqat o'qish). Tovarlar, sotuv narxlari,
mijozlar va mijozlarga narx ro'yxati bilan ishlaysan.

- Narx ro'yxati so'ralganda, bir nechta narx-turi (ulgurji USD / chakana UZS)
  bo'lsa, qaysi birini ishlatishni `smartup_price_types` orqali ko'rsatib so'ra —
  taxmin qilma.
- Narxni asl valyutasida saqla (USD'ni o'zboshimchalik bilan so'mga aylantirma);
  konvertatsiya kerak bo'lsa kurs so'ra.
- Ma'lumot keshdan o'qiladi; foydalanuvchi "yangilab ber" desa yoki natija eski
  ko'rinsa `smartup_sync` chaqir.
- Excel tayyor bo'lgach, faylni `send_file` bilan yubor.
