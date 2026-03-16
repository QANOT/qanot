# ibox.io Tools

ibox.io ombor boshqaruv tizimi bilan integratsiya. Tovarlar, qoldiq, sotuvlar, xaridlar, to'lovlar va hisobotlar.

## Tovarlar
- `ibox_search_products` — Tovar qidirish (nomi, shtrix-kod, SKU)
- `ibox_get_product` — Bitta tovar tafsilotlari
- `ibox_get_categories` — Kategoriyalar
- `ibox_get_brands` — Brendlar
- `ibox_get_units` — O'lchov birliklari

## Ombor / Qoldiq
- `ibox_get_stock` — Ombordagi qoldiq
- `ibox_get_stock_by_product` — Bitta tovar qoldig'i (barcha omborlarda)
- `ibox_get_stock_by_warehouse` — Bitta ombordagi barcha tovarlar
- `ibox_get_warehouses` — Omborlar ro'yxati

## Sotuvlar
- `ibox_get_orders` — Buyurtmalar ro'yxati
- `ibox_get_order` — Bitta buyurtma tafsilotlari
- `ibox_get_sales_by_product` — Tovar bo'yicha sotuv
- `ibox_get_shipments` — Yetkazib berish hisoboti

## Xaridlar
- `ibox_get_purchases` — Xaridlar hisoboti
- `ibox_get_purchase_returns` — Qaytarilgan xaridlar

## To'lovlar
- `ibox_get_payments_received` — Qabul qilingan to'lovlar
- `ibox_get_payments_made` — Qilingan to'lovlar
- `ibox_get_installments` — Nasiyalar

## Hisobotlar
- `ibox_get_dashboard` — Umumiy statistika
- `ibox_get_profit_loss` — Foyda va zarar
- `ibox_get_profitability` — Rentabellik
- `ibox_get_abc_analysis` — ABC tahlil
- `ibox_get_days_in_stock` — Omborda yotish muddati

## Mijozlar
- `ibox_get_customers` — Mijozlar hisoboti
- `ibox_get_outlets` — Savdo nuqtalari
- `ibox_get_customer_daily` — Kunlik hisobot

## Qoidalar
- Narxlarni so'm (UZS) formatida ko'rsat
- Qoldiq so'raganda `ibox_get_stock` ishlatiladi
- Dashboard uchun default `filter_by: month`
- Texnik tafsilotlarni (API, token) foydalanuvchiga ko'rsatma
