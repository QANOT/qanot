# TOOLS.md - Tool Configuration & Notes

## Bito POS/ERP Integration (bito_*)

Siz Bito POS/ERP tizimiga to'g'ridan-to'g'ri ulangansiz. 20 ta bito_* tool mavjud.

**MUHIM:** Login ma'lumotlari OLDINDAN sozlangan. Foydalanuvchidan HECH QACHON token, parol yoki API kalit so'ramang. Tizimga kirish avtomatik amalga oshiriladi.

### Sotuvlar (Savdo):
- `bito_get_sales` — Sotuvlar ro'yxati. Sana va tovar bo'yicha filter.
- `bito_get_sale` — Bitta sotuv tafsilotlari (ID bo'yicha).
- `bito_create_sale` — Yangi sotuv yaratish (tovarlar, mijoz, ombor).

### Tovarlar:
- `bito_get_products` — Tovarlar ro'yxati. Nomi yoki kategoriya bo'yicha qidirish.
- `bito_get_product` — Bitta tovar tafsilotlari (ID bo'yicha).
- `bito_create_product` — Yangi tovar yaratish (nomi, narxi, kategoriya).

### Mijozlar:
- `bito_get_customers` — Mijozlar ro'yxati. Ism yoki telefon bo'yicha qidirish.
- `bito_get_customer` — Bitta mijoz tafsilotlari (ID bo'yicha).
- `bito_create_customer` — Yangi mijoz yaratish (ism, telefon, manzil).

### Ombor (Zaxira):
- `bito_get_warehouses` — Barcha omborlar ro'yxati.
- `bito_get_stock` — Tovarlar qoldiq ma'lumotlari (ombordagi zaxira).

### Xaridlar (Kirim):
- `bito_get_purchases` — Xaridlar (kirim) ro'yxati. Sana bo'yicha filter.
- `bito_get_purchase` — Bitta xarid (kirim) tafsilotlari (ID bo'yicha).

### Buyurtmalar:
- `bito_get_orders` — Buyurtmalar ro'yxati.
- `bito_get_order` — Bitta buyurtma tafsilotlari (ID bo'yicha).
- `bito_create_order` — Yangi buyurtma yaratish.

### Ta'minotchilar:
- `bito_get_suppliers` — Ta'minotchilar ro'yxati.

### Hisobotlar:
- `bito_get_sales_summary` — Sotuv hisoboti (umumiy statistika).
- `bito_get_sales_by_product` — Tovar bo'yicha sotuv hisoboti.

### Akkaunt:
- `bito_get_profile` — Akkaunt ma'lumotlari.

### Foydalanish misollari:

**Sotuvlar bilan ishlash:**
```
# Barcha sotuvlarni ko'rish
bito_get_sales()

# Ma'lum sanadagi sotuvlar
bito_get_sales(from_date="2026-03-01", to_date="2026-03-15")

# Sotuv yaratish
bito_create_sale(customer_id=1, warehouse_id=1, items=[{"product_id": 5, "quantity": 2, "price": 50000}])
```

**Tovarlar:**
```
# Tovarlarni qidirish
bito_get_products(search="telefon")

# Tovar yaratish
bito_create_product(name="Samsung Galaxy A54", price=3500000, category_id=1)

# Tovar qoldig'ini tekshirish
bito_get_stock(product_id=5)
```

**Mijozlar:**
```
# Mijozlarni qidirish
bito_get_customers(search="Alisher")

# Mijoz yaratish
bito_create_customer(name="Alisher Karimov", phone="+998901234567")
```

**Buyurtmalar:**
```
# Buyurtmalar ro'yxati
bito_get_orders()

# Buyurtma yaratish
bito_create_order(customer_id=1, warehouse_id=1, items=[{"product_id": 5, "quantity": 3, "price": 50000}])
```

**Hisobotlar:**
```
# Sotuv hisoboti
bito_get_sales_summary(from_date="2026-03-01", to_date="2026-03-15")

# Tovar bo'yicha sotuv
bito_get_sales_by_product(from_date="2026-03-01", to_date="2026-03-15")
```

**Ombor:**
```
# Omborlar ro'yxati
bito_get_warehouses()

# Tovar qoldiqlari
bito_get_stock(warehouse_id=1)
```

### Qoidalar:
- **MUHIM: Foydalanuvchiga API, token, endpoint, JSON kabi texnik so'zlarni HECH QACHON aytmang. Shunchaki natijani taqdim eting.**
- Login ma'lumotlarini so'ramang — avtomatik.
- Sotuv yaratganda avval `bito_get_products` bilan tovarni, `bito_get_customers` bilan mijozni tekshiring.
- Tovar qoldig'ini bilish uchun `bito_get_stock` dan foydalaning.
- Narxlarni so'm da ko'rsating.
- Sahifalash: page=0 birinchi sahifa, size=20 default.
