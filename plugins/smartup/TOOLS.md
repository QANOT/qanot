# Smartup ERP

Smartup ERP tizimi bilan integratsiya (faqat o'qish). Tovarlar, sotuv narxlari,
mijozlar va mijozlarga **Excel narx ro'yxati** tayyorlash.

## Ishlash tartibi

Smartup'dan ma'lumot olish og'ir va so'rov limiti bor, shuning uchun ma'lumot
**lokal keshga** yuklab olinadi. Boshqa tool'lar shu keshdan o'qiydi.

1. **Birinchi marta** yoki ma'lumot eskirganda → `smartup_sync` chaqiring.
2. Keyin `smartup_search_products`, `smartup_categories`, `smartup_price_types`,
   `smartup_pricelist`, `smartup_customers` keshdan tez ishlaydi.
3. Kesh holatini `smartup_status` bilan ko'ring.

## Narxlar qayerdan olinadi

Sotuv narxlari **sotuv hujjatlaridan** (`order$export`) olinadi — har tovar so'nggi
sotilgan narxi (narx-turi bo'yicha: masalan ulgurji USD, chakana UZS) saqlanadi.
Narx **asl valyutada** qoladi (USD'ni o'zboshimchalik bilan so'mga aylantirmang).

## Tool'lar

| Tool | Vazifa |
|---|---|
| `smartup_sync` | Smartup'dan keshga yuklab olish. `what`: all\|catalog\|prices\|customers. `price_days` — sotuv narxi uchun necha kun orqaga (default 90). |
| `smartup_status` | Kesh holati: nechta yozuv va oxirgi yangilanish vaqti. |
| `smartup_categories` | Kategoriyalar ro'yxati (har birida tovar soni). |
| `smartup_price_types` | Mavjud sotuv narx-turlari va har biriga narxi bor tovarlar soni. |
| `smartup_search_products` | Tovarlarni nom/artikul/shtrix-kod/kategoriya bo'yicha qidirish + sotuv narxlari. |
| `smartup_pricelist` | **Excel narx ro'yxati** — tovarlar + sotuv narxi (+ ixtiyoriy ustama %). |
| `smartup_customers` | Mijoz/ta'minotchilar (yuridik/jismoniy). |
| `smartup_balance` | Ombor qoldig'i (jonli, filial_code + sana oralig'i <31 kun). |

## Narx ro'yxati (pricelist) — eng muhim ssenariy

Foydalanuvchi "bambuk kategoriyasidagi tovarlarni narx ro'yxati qilib ber" desa:

1. Kesh bormi tekshiring (`smartup_status`); bo'sh/eski bo'lsa `smartup_sync`.
2. Bir nechta narx-turi bo'lishi mumkin (ulgurji USD / chakana UZS). Noaniq bo'lsa
   `smartup_price_types` ko'rsating va foydalanuvchidan qaysi narx-turi kerakligini so'rang.
3. `smartup_pricelist(query="bambuk", price_type_code=..., markup_percent=0)`.
   - "bambuk" alohida kategoriya emas — `query` nom bo'yicha qidiradi. Aniq
     kategoriya bo'lsa `category=` ishlating.
   - Ustama kerak bo'lsa `markup_percent` qo'shing (masalan 30). Ustamasiz = sof sotuv narxi.
4. Natijadagi `file_path`ni **`send_file`** bilan yuboring.

`round_to` bilan yakuniy narxni yaxlitlash mumkin (masalan 1000 so'mgacha).

## Cheklovlar

- Faqat o'qish — hech narsa o'zgartirmaydi/yozmaydi.
- Sotuv oralig'ida (default 90 kun) sotilmagan tovarlarda narx bo'lmaydi —
  `without_price` sonida ko'rsatiladi. Kerak bo'lsa `smartup_sync(what="prices",
  price_days=180)` bilan oraliqni kengaytiring.
