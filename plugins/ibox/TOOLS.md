# TOOLS.md - Tool Configuration & Notes

## ibox.io Ombor Integration (ibox_*)

Siz ibox.io ombor boshqaruv tizimiga to'g'ridan-to'g'ri ulangansiz. 27 ta ibox_* tool mavjud.

**MUHIM:** Login ma'lumotlari OLDINDAN sozlangan. Foydalanuvchidan HECH QACHON login, parol yoki tenant so'ramang. Tizimga kirish avtomatik amalga oshiriladi.

### Tovarlar:
- `ibox_search_products` — Tovarlar ro'yxati. Nomi, shtrix-kod, SKU bo'yicha qidirish.
- `ibox_get_product` — Bitta tovar tafsilotlari (ID bo'yicha).
- `ibox_get_categories` — Tovar kategoriyalari ro'yxati.
- `ibox_get_brands` — Tovar brendlari ro'yxati.
- `ibox_get_units` — O'lchov birliklari (dona, kg, litr).

### Ombor / Qoldiq:
- `ibox_get_stock` — Ombordagi tovarlar qoldig'i. Tovar, ombor, kategoriya bo'yicha filter.
- `ibox_get_stock_by_product` — Bitta tovar bo'yicha barcha ombordagi qoldiq.
- `ibox_get_stock_by_warehouse` — Bitta ombor bo'yicha barcha tovarlar qoldig'i.
- `ibox_get_warehouses` — Omborlar ro'yxati.

### Sotuvlar:
- `ibox_get_orders` — Buyurtmalar (sotuvlar) ro'yxati. Sana, mijoz bo'yicha filter.
- `ibox_get_order` — Bitta buyurtma tafsilotlari (ID bo'yicha).
- `ibox_get_sales_by_product` — Tovar bo'yicha sotuv hisoboti.
- `ibox_get_shipments` — Yetkazib berish (jo'natish) hisoboti.

### Xaridlar:
- `ibox_get_purchases` — Xaridlar hisoboti. Sana bo'yicha filter.
- `ibox_get_purchase_returns` — Qaytarilgan xaridlar hisoboti.

### To'lovlar:
- `ibox_get_payments_received` — Mijozlardan qabul qilingan to'lovlar.
- `ibox_get_payments_made` — Ta'minotchilarga qilingan to'lovlar.
- `ibox_get_installments` — Nasiya (bo'lib to'lash) ro'yxati.

### Hisobotlar:
- `ibox_get_dashboard` — Umumiy dashboard — sotuv, xarid, foyda statistikasi.
- `ibox_get_profit_loss` — Foyda va zarar hisoboti.
- `ibox_get_profitability` — Rentabellik hisoboti (tovar/kategoriya bo'yicha).
- `ibox_get_abc_analysis` — ABC tahlil — tovarlarni A/B/C guruhga ajratish.
- `ibox_get_days_in_stock` — Omborda necha kun yotganligi hisoboti.

### Mijozlar:
- `ibox_get_customers` — Mijozlar hisoboti — qarz, to'lov, buyurtma statistikasi.
- `ibox_get_outlets` — Savdo nuqtalari (do'konlar) ro'yxati.
- `ibox_get_customer_daily` — Mijoz kunlik hisoboti.

### Umumiy:
- `ibox_get_profile` — Akkaunt ma'lumotlari — kim sifatida ulangan.

### Ishlatish namunalari:
- "Omborda grafin bormi?" → `ibox_get_stock` bilan qidiring
- "Bugun qancha savdo bo'ldi?" → `ibox_get_dashboard` (filter_by: today)
- "Eng ko'p sotiladigan tovar?" → `ibox_get_sales_by_product`
- "Mijoz qarzi qancha?" → `ibox_get_customers`
