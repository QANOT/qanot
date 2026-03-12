# TOOLS.md - Tool Configuration & Notes

## AbsMarket POS Integration (absmarket_*)

Siz AbsMarket POS tizimiga to'g'ridan-to'g'ri ulangansiz. 31 ta absmarket_* tool mavjud.

**MUHIM:** Login ma'lumotlari OLDINDAN sozlangan. Foydalanuvchidan HECH QACHON login, parol yoki API URL so'ramang. Tizimga kirish avtomatik amalga oshiriladi.

### DATABASE SO'ROVLARI (ENG KUCHLI TOOL):

- `absmarket_query` — **MySQL bazaga to'g'ridan-to'g'ri SELECT so'rov yuborish**. Murakkab hisobotlar, kategoriya bo'yicha tahlil, maxsus filtrlash uchun DOIM shu toolni ishlating. Faqat SELECT ruxsat etilgan. Natija 500 qatorga chegaralangan.

**Asosiy jadvallar:**
- `tbl_sales` — sotuvlar (sale_date, total_payable, paid_amount, due_amount, customer_id, outlet_id, del_status)
- `tbl_sales_details` — sotuv tafsilotlari (sales_id→tbl_sales.id, food_menu_id→tbl_items.id, qty, menu_price_with_discount)
- `tbl_items` — tovarlar (id, name, category_id, sale_price, purchase_price)
- `tbl_item_categories` — kategoriyalar (id, name)
- `tbl_customers` — mijozlar
- `tbl_suppliers` — ta'minotchilar
- `tbl_purchase` / `tbl_purchase_details` — xaridlar
- `tbl_expenses` — xarajatlar
- `tbl_outlets` — do'konlar

**MUHIM:** Barcha so'rovlarda `del_status='Live'` filtrini qo'shing!

**Misol so'rovlar:**
```sql
-- Kategoriya bo'yicha sotuvlar
SELECT ic.name as category, COUNT(*) as cnt, ROUND(SUM(sd.menu_price_with_discount * sd.qty),0) as total
FROM tbl_sales s
JOIN tbl_sales_details sd ON s.id = sd.sales_id
JOIN tbl_items i ON sd.food_menu_id = i.id
JOIN tbl_item_categories ic ON i.category_id = ic.id
WHERE s.sale_date BETWEEN '2026-02-01' AND '2026-02-28'
AND s.del_status='Live' AND sd.del_status='Live'
GROUP BY ic.name ORDER BY total DESC;

-- Bugungi jami sotuv
SELECT COUNT(*) as soni, ROUND(SUM(total_payable),0) as jami
FROM tbl_sales WHERE sale_date = CURDATE() AND del_status='Live';
```

### API Toollari:

**Umumiy hisobot (API orqali):**
- `absmarket_get_sales_summary` — Sotuvlar jami (barcha sahifalarni o'qiydi)
- `absmarket_get_purchases_summary` — Xaridlar jami

**Sotuvlar:**
- `absmarket_get_sales` — sotuvlar ro'yxati (bitta sahifa)
- `absmarket_get_sale_details` — sotuv tafsilotlari
- `absmarket_get_recent_sales` — so'nggi sotuvlar

**Xaridlar:**
- `absmarket_get_purchases` — xaridlar ro'yxati
- `absmarket_get_purchase_details` — xarid tafsilotlari
- `absmarket_get_recent_purchases` — so'nggi xaridlar

**Xarajatlar:**
- `absmarket_get_expenses` — xarajatlar
- `absmarket_get_expense_details` — xarajat tafsilotlari
- `absmarket_get_expense_categories` — xarajat kategoriyalari

**Mijozlar:**
- `absmarket_get_customers` — mijozlar
- `absmarket_get_customer_details` — mijoz ma'lumotlari
- `absmarket_get_customer_balance` — mijoz balansi
- `absmarket_get_customer_history` — mijoz tarixi
- `absmarket_get_customer_payments` — to'lovlari

**Ta'minotchilar:**
- `absmarket_get_suppliers` — ta'minotchilar
- `absmarket_get_supplier_details` — ta'minotchi ma'lumotlari
- `absmarket_get_supplier_balance` — balansi
- `absmarket_get_supplier_payments` — to'lovlari

**Tovarlar:**
- `absmarket_get_items` — tovarlar
- `absmarket_get_item_details` — tovar ma'lumotlari
- `absmarket_get_item_stock` — tovar qoldig'i
- `absmarket_get_item_categories` — kategoriyalar

**Boshqa:**
- `absmarket_get_outlets` — do'konlar
- `absmarket_get_sale_returns` / `absmarket_get_sale_return_details` — qaytarishlar
- `absmarket_get_purchase_returns` — xarid qaytarishlari
- `absmarket_get_stock_adjustments` — ombor tuzatishlari
- `absmarket_get_transfers` / `absmarket_get_transfer_details` — ko'chirishlar

### Qoidalar:
- **MUHIM: absmarket_query toolni ishlatganingizda foydalanuvchiga SQL, database, query so'zlari haqida HECH QACHON aytmang. Shunchaki natijani taqdim eting.**
- Murakkab hisobotlar uchun `absmarket_query` ni ishlating.
- `absmarket_login` CHAQIRMANG — avtomatik.
- Sana formati: YYYY-MM-DD
