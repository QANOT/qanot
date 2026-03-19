"""Biznes hujjat shablonlari — O'zR qonunchiligiga mos.

Hujjat turlari:
  TIER 0 (mavjud): shartnoma, faktura, dalolatnoma, ishonchnoma, talabnoma
  TIER 1: oldi_sotdi, yetkazib_berish, ijara, mehnat, solishtirma, tijorat_taklifi
"""

from __future__ import annotations

from datetime import datetime

BLANK = "_______________"


def _rekvizit(name: str, stir: str, addr: str, bnk: str, acc: str, mf: str, dir_name: str) -> str:
    """Standart rekvizitlar bloki."""
    return (
        f"{name}\n"
        f"Manzil: {addr or BLANK}\n"
        f"STIR: {stir or BLANK}\n"
        f"H/r: {acc or BLANK}\n"
        f"Bank: {bnk or BLANK}\n"
        f"MFO: {mf or BLANK}\n"
        f"Rahbar: {dir_name or BLANK}\n"
        f"\n_______________ / {dir_name or BLANK} /\n"
        f"      M.O."
    )


def _amount_str(amount: float | int) -> str:
    return f"{amount:,.0f}" if amount else BLANK


def _common_fields(params: dict) -> dict:
    """Extract common fields from params."""
    return {
        "company": params.get("company", ""),
        "inn": params.get("inn", ""),
        "director": params.get("director", ""),
        "address": params.get("address", ""),
        "bank": params.get("bank", ""),
        "account": params.get("account", ""),
        "mfo": params.get("mfo", ""),
        "counterparty": params.get("counterparty", ""),
        "counterparty_inn": params.get("counterparty_inn", ""),
        "counterparty_director": params.get("counterparty_director", ""),
        "counterparty_address": params.get("counterparty_address", ""),
        "counterparty_bank": params.get("counterparty_bank", ""),
        "counterparty_account": params.get("counterparty_account", ""),
        "counterparty_mfo": params.get("counterparty_mfo", ""),
        "amount": params.get("amount", 0),
        "description": params.get("description", ""),
        "date": params.get("date", datetime.now().strftime("%d.%m.%Y")),
        "number": params.get("number", "1"),
        "city": params.get("city", "Toshkent"),
    }


# ═══════════════════════════════════════════════════════════
# OLDI-SOTDI SHARTNOMASI (FK 386-432)
# ═══════════════════════════════════════════════════════════

def generate_oldi_sotdi(params: dict) -> str:
    """Oldi-sotdi shartnomasi — FK 386-432.

    Umumiy shartnomadan farqi:
    - Tovar tavsifi majburiy (FK 396-398: nomi, soni, sifati)
    - Mulk huquqi o'tishi (FK 392)
    - Kafolat muddati (FK 405-416)
    - QQS alohida ko'rsatiladi
    """
    f = _common_fields(params)
    items = params.get("items", [])
    delivery_date = params.get("delivery_date", BLANK)
    delivery_place = params.get("delivery_place", BLANK)
    warranty_months = params.get("warranty_months", 0)
    payment_type = params.get("payment_type", "bank")  # bank, cash, mixed
    prepay_pct = params.get("prepay_pct", 0)
    valid_until = params.get("valid_until", BLANK)

    amount = f["amount"]
    amount_s = _amount_str(amount)

    # Tovarlar jadvali
    items_text = ""
    if items:
        items_text = "No | Nomi | O'lchov | Soni | Narx (so'm) | Jami (so'm)\n"
        items_text += "=" * 70 + "\n"
        total = 0
        for i, item in enumerate(items, 1):
            name = item.get("name", "")
            qty = item.get("quantity", 1)
            unit = item.get("unit", "dona")
            price = item.get("price", 0)
            summa = qty * price
            total += summa
            items_text += f"{i}. {name} | {unit} | {qty} | {price:,.0f} | {summa:,.0f}\n"
        items_text += "=" * 70 + "\n"
        items_text += f"Jami: {total:,.0f} so'm\n"
        if amount == 0:
            amount = total
            amount_s = _amount_str(amount)

    # To'lov tartibi
    if payment_type == "cash":
        payment_text = "To'lov naqd pul orqali amalga oshiriladi."
    elif payment_type == "mixed":
        payment_text = (
            f"To'lov aralash usulda amalga oshiriladi:\n"
            f"  a) {prepay_pct}% oldindan to'lov — shartnoma imzolangan kundan 3 bank kuni ichida;\n"
            f"  b) qolgan qism — tovar topshirilganidan keyin 5 bank kuni ichida."
        )
    else:
        if prepay_pct:
            payment_text = (
                f"To'lov bank o'tkazmasi orqali:\n"
                f"  a) {prepay_pct}% oldindan to'lov — shartnoma imzolangan kundan 3 bank kuni ichida;\n"
                f"  b) qolgan {100 - prepay_pct}% — tovar topshirilganidan keyin 5 bank kuni ichida."
            )
        else:
            payment_text = "To'lov bank o'tkazmasi orqali tovar topshirilganidan keyin 5 (besh) bank kuni ichida amalga oshiriladi."

    content = (
        f"OLDI-SOTDI SHARTNOMASI No {f['number']}\n"
        f"(O'zR Fuqarolik Kodeksining 386-432-moddalari asosida)\n\n"
        f"{f['city']} shahri{' ' * 40}{f['date']}\n\n"
        f"{f['company']} (keyingi o'rinlarda \"Sotuvchi\" deb yuritiladi), "
        f"STIR {f['inn'] or BLANK}, rahbar {f['director'] or BLANK} shaxsida, "
        f"Ustav asosida faoliyat yurituvchi, bir tomondan, va\n\n"
        f"{f['counterparty']} (keyingi o'rinlarda \"Xaridor\" deb yuritiladi), "
        f"STIR {f['counterparty_inn'] or BLANK}, rahbar {f['counterparty_director'] or BLANK} shaxsida, "
        f"Ustav asosida faoliyat yurituvchi, ikkinchi tomondan,\n\n"
        f"quyidagilar to'g'risida mazkur shartnomani tuzdilar:\n\n"
        #
        f"1. SHARTNOMA PREDMETI\n\n"
        f"1.1. Sotuvchi o'z mulki bo'lgan tovarni Xaridorga sotishni, "
        f"Xaridor esa uni qabul qilib to'lashni o'z zimmasiga oladi.\n"
        f"1.2. Tovar tavsifi:\n"
        f"{f['description'] or BLANK}\n\n"
    )

    if items_text:
        content += f"1.3. Tovarlar ro'yxati:\n{items_text}\n"

    content += (
        f"1.4. Shartnomaning umumiy qiymati: {amount_s} so'm "
        f"(QQS {round(amount * 12 / 100):,.0f} so'm alohida).\n\n"
        #
        f"2. TOVAR SIFATI VA MIQDORI (FK 396-404)\n\n"
        f"2.1. Tovar tegishli sifat sertifikati va standartlarga mos bo'lishi shart.\n"
        f"2.2. Miqdori shartnoma va ilovada ko'rsatilgan hajmda.\n"
        f"2.3. Sifatga oid da'vo — qabul qilish aktiga kiritiladi.\n\n"
        #
        f"3. YETKAZIB BERISH (FK 402)\n\n"
        f"3.1. Yetkazib berish muddati: {delivery_date}.\n"
        f"3.2. Yetkazib berish joyi: {delivery_place}.\n"
        f"3.3. Transportga sarflangan xarajatlar Sotuvchi zimmasida.\n"
        f"3.4. Tovar bilan birga: hisob-faktura, sifat sertifikati, yuk xati (TTN).\n\n"
        #
        f"4. MULK HUQUQI O'TISHI (FK 392)\n\n"
        f"4.1. Tovar ustidagi mulk huquqi qabul-topshirish dalolatnomasi "
        f"imzolangan paytdan boshlab Xaridorga o'tadi.\n"
        f"4.2. Tovarning tasodifan yo'qolishi yoki shikastlanishi xavfi "
        f"mulk huquqi o'tgan paytdan boshlab Xaridorga o'tadi.\n\n"
        #
        f"5. NARX VA TO'LOV TARTIBI\n\n"
        f"5.1. Shartnoma narxi: {amount_s} so'm.\n"
        f"5.2. {payment_text}\n\n"
    )

    if warranty_months:
        content += (
            f"6. KAFOLAT (FK 405-416)\n\n"
            f"6.1. Kafolat muddati: tovar topshirilgan kundan boshlab "
            f"{warranty_months} oy.\n"
            f"6.2. Kafolat davomida aniqlangan kamchiliklar Sotuvchi hisobidan "
            f"bartaraf etiladi.\n"
            f"6.3. Xaridor kamchilik aniqlangan kundan boshlab 10 kun ichida "
            f"Sotuvchiga yozma xabar berishi shart.\n\n"
            f"7. JAVOBGARLIK\n\n"
        )
    else:
        content += f"6. JAVOBGARLIK\n\n"

    section_n = 7 if warranty_months else 6
    content += (
        f"{section_n}.1. Tomonlar o'z majburiyatlarini bajarmagan taqdirda "
        f"O'zR FK ga muvofiq javobgar bo'ladilar.\n"
        f"{section_n}.2. Yetkazib berish kechiktirilsa — har bir kun uchun "
        f"shartnoma summasining 0,5% miqdorida penya, lekin 50% dan oshmasligi kerak.\n"
        f"{section_n}.3. To'lov kechiktirilsa — har bir kun uchun qarz summasining "
        f"0,04% miqdorida penya.\n\n"
    )
    section_n += 1
    content += (
        f"{section_n}. FORS-MAJOR HOLATLARI\n\n"
        f"{section_n}.1. Tabiiy ofatlar, urush, davlat qarorlari kabi "
        f"tomonlar nazorat qila olmaydigan holatlar fors-major hisoblanadi.\n"
        f"{section_n}.2. Fors-major holati yuzaga kelganda tomon 3 kun ichida "
        f"ikkinchi tomonga yozma xabar berishi shart.\n\n"
    )
    section_n += 1
    content += (
        f"{section_n}. NIZOLARNI HAL QILISH\n\n"
        f"{section_n}.1. Nizolar muzokaralar yo'li bilan hal qilinadi.\n"
        f"{section_n}.2. Kelishuvga erishilmagan taqdirda — sudgacha talabnoma "
        f"(10 kun javob muddati) majburiy (IPK 187-modda).\n"
        f"{section_n}.3. Nizo iqtisodiy sudda ko'riladi.\n\n"
    )
    section_n += 1
    content += (
        f"{section_n}. SHARTNOMA MUDDATI VA YAKUNIY QOIDALAR\n\n"
        f"{section_n}.1. Shartnoma imzolangan kundan boshlab kuchga kiradi.\n"
        f"{section_n}.2. Shartnoma muddati: {valid_until} gacha.\n"
        f"{section_n}.3. Shartnomaga o'zgartirish faqat yozma kelishuv bilan.\n"
        f"{section_n}.4. Shartnoma 2 nusxada tuzilgan.\n"
        f"{section_n}.5. Ko'rsatilmagan masalalar O'zR FK ga muvofiq tartibga solinadi.\n\n"
    )
    content += (
        f"TOMONLAR REKVIZITLARI VA IMZOLARI\n\n"
        f"Sotuvchi:\n{_rekvizit(f['company'], f['inn'], f['address'], f['bank'], f['account'], f['mfo'], f['director'])}\n\n"
        f"Xaridor:\n{_rekvizit(f['counterparty'], f['counterparty_inn'], f['counterparty_address'], f['counterparty_bank'], f['counterparty_account'], f['counterparty_mfo'], f['counterparty_director'])}"
    )
    return content


# ═══════════════════════════════════════════════════════════
# YETKAZIB BERISH SHARTNOMASI (FK 437-462)
# ═══════════════════════════════════════════════════════════

def generate_yetkazib_berish(params: dict) -> str:
    """Yetkazib berish shartnomasi — FK 437-462.

    Oldi-sotdidan farqi:
    - Tadbirkorlik faoliyati doirasida (FK 437)
    - Muntazam yetkazib berish jadvali (FK 441)
    - Qabul qilish tartibi batafsil (FK 443-445)
    - Kamchilik aniqlanganda ogohlantirish (FK 449)
    """
    f = _common_fields(params)
    items = params.get("items", [])
    delivery_schedule = params.get("delivery_schedule", "har oyda")
    delivery_place = params.get("delivery_place", BLANK)
    acceptance_days = params.get("acceptance_days", 3)
    valid_until = params.get("valid_until", BLANK)
    payment_days = params.get("payment_days", 5)

    amount = f["amount"]
    amount_s = _amount_str(amount)

    # Tovarlar jadvali
    items_text = ""
    if items:
        items_text = "No | Nomi | O'lchov | Soni | Narx (so'm) | Jami (so'm)\n"
        items_text += "=" * 70 + "\n"
        total = 0
        for i, item in enumerate(items, 1):
            name = item.get("name", "")
            qty = item.get("quantity", 1)
            unit = item.get("unit", "dona")
            price = item.get("price", 0)
            summa = qty * price
            total += summa
            items_text += f"{i}. {name} | {unit} | {qty} | {price:,.0f} | {summa:,.0f}\n"
        items_text += "=" * 70 + "\n"
        items_text += f"Jami: {total:,.0f} so'm\n"
        if amount == 0:
            amount = total
            amount_s = _amount_str(amount)

    content = (
        f"YETKAZIB BERISH SHARTNOMASI No {f['number']}\n"
        f"(O'zR Fuqarolik Kodeksining 437-462-moddalari asosida)\n\n"
        f"{f['city']} shahri{' ' * 40}{f['date']}\n\n"
        f"{f['company']} (keyingi o'rinlarda \"Yetkazib beruvchi\" deb yuritiladi), "
        f"STIR {f['inn'] or BLANK}, rahbar {f['director'] or BLANK} shaxsida, "
        f"Ustav asosida faoliyat yurituvchi, bir tomondan, va\n\n"
        f"{f['counterparty']} (keyingi o'rinlarda \"Buyurtmachi\" deb yuritiladi), "
        f"STIR {f['counterparty_inn'] or BLANK}, rahbar {f['counterparty_director'] or BLANK} shaxsida, "
        f"Ustav asosida faoliyat yurituvchi, ikkinchi tomondan,\n\n"
        f"O'zR FK 437-moddasi asosida quyidagi shartnomani tuzdilar:\n\n"
        #
        f"1. SHARTNOMA PREDMETI\n\n"
        f"1.1. Yetkazib beruvchi tadbirkorlik faoliyati doirasida ishlab chiqargan "
        f"yoki sotib olgan tovarlarni Buyurtmachiga yetkazib berishni, "
        f"Buyurtmachi esa ularni qabul qilib to'lashni o'z zimmasiga oladi.\n"
        f"1.2. Tovar tavsifi: {f['description'] or BLANK}\n\n"
    )

    if items_text:
        content += f"1.3. Tovarlar ro'yxati:\n{items_text}\n"

    content += (
        f"1.4. Shartnoma umumiy qiymati: {amount_s} so'm (QQS alohida).\n\n"
        #
        f"2. YETKAZIB BERISH TARTIBI (FK 441-442)\n\n"
        f"2.1. Yetkazib berish jadvali: {delivery_schedule}.\n"
        f"2.2. Yetkazib berish joyi: {delivery_place}.\n"
        f"2.3. Yetkazib beruvchi yetkazib berish sanasi haqida kamida 2 kun oldin xabar beradi.\n"
        f"2.4. Tovar bilan birga: hisob-faktura, yuk xati (TTN), sifat sertifikati.\n"
        f"2.5. O'rash va qadoqlash — O'zR standartlari va texnik shartlarga mos bo'lishi kerak.\n\n"
        #
        f"3. TOVAR SIFATI VA QABUL QILISH (FK 443-445)\n\n"
        f"3.1. Tovar O'zR amaldagi standartlari va texnik shartlarga mos bo'lishi kerak.\n"
        f"3.2. Buyurtmachi tovarni qabul qilish paytida miqdor va sifatni tekshiradi.\n"
        f"3.3. Tekshirish muddati: tovar kelgan kundan boshlab {acceptance_days} ish kuni.\n"
        f"3.4. Kamchilik aniqlansa — Buyurtmachi darhol (24 soat ichida) "
        f"Yetkazib beruvchiga yozma xabar beradi (FK 449).\n"
        f"3.5. Sifatsiz tovar Yetkazib beruvchi hisobidan almashtiriladi yoki qaytariladi.\n\n"
        #
        f"4. NARX VA TO'LOV TARTIBI\n\n"
        f"4.1. Shartnoma narxi: {amount_s} so'm.\n"
        f"4.2. To'lov bank o'tkazmasi orqali har bir partiya uchun alohida — "
        f"tovar qabul qilinganidan keyin {payment_days} bank kuni ichida.\n"
        f"4.3. To'lov uchun asos: hisob-faktura va qabul dalolatnomasi.\n\n"
        #
        f"5. TOMONLARNING MAJBURIYATLARI\n\n"
        f"5.1. Yetkazib beruvchi:\n"
        f"  a) tovarni belgilangan jadval va miqdorda yetkazib berish;\n"
        f"  b) tovar sifati va xavfsizligini ta'minlash;\n"
        f"  c) zarur hujjatlarni taqdim etish (sertifikat, TTN, faktura);\n"
        f"  d) kamchilik bo'yicha da'volarni o'z vaqtida ko'rib chiqish.\n\n"
        f"5.2. Buyurtmachi:\n"
        f"  a) tovarni o'z vaqtida qabul qilish va tekshirish;\n"
        f"  b) to'lovni belgilangan muddatda amalga oshirish;\n"
        f"  c) kamchilik aniqlansa darhol Yetkazib beruvchini xabardor qilish;\n"
        f"  d) tovarni saqlash sharoitlarini ta'minlash.\n\n"
        #
        f"6. JAVOBGARLIK\n\n"
        f"6.1. Yetkazib berish kechiktirilsa — har bir kun uchun kechiktirilgan "
        f"partiya qiymatining 0,5% miqdorida penya (FK 460).\n"
        f"6.2. To'lov kechiktirilsa — har bir kun uchun qarz summasining "
        f"0,04% miqdorida penya.\n"
        f"6.3. Kam yetkazilgan tovar uchun — Yetkazib beruvchi keyingi partiyada "
        f"to'ldirishi yoki qaytarishi shart (FK 450).\n"
        f"6.4. Penya miqdori shartnoma summasining 50% dan oshmasligi kerak.\n\n"
        #
        f"7. FORS-MAJOR\n\n"
        f"7.1. Tabiiy ofatlar, urush, davlat qarorlari — fors-major hisoblanadi.\n"
        f"7.2. Fors-major yuz berganda tomon 3 kun ichida yozma xabar beradi.\n\n"
        #
        f"8. NIZOLARNI HAL QILISH\n\n"
        f"8.1. Nizolar muzokaralar yo'li bilan hal qilinadi.\n"
        f"8.2. Sudgacha talabnoma (10 kun javob muddati) majburiy.\n"
        f"8.3. Nizo iqtisodiy sudda ko'riladi.\n\n"
        #
        f"9. SHARTNOMA MUDDATI\n\n"
        f"9.1. Shartnoma imzolangan kundan boshlab {valid_until} gacha amal qiladi.\n"
        f"9.2. Shartnoma 2 nusxada tuzilgan.\n"
        f"9.3. O'zgartirish faqat yozma kelishuv bilan.\n\n"
        #
        f"TOMONLAR REKVIZITLARI VA IMZOLARI\n\n"
        f"Yetkazib beruvchi:\n{_rekvizit(f['company'], f['inn'], f['address'], f['bank'], f['account'], f['mfo'], f['director'])}\n\n"
        f"Buyurtmachi:\n{_rekvizit(f['counterparty'], f['counterparty_inn'], f['counterparty_address'], f['counterparty_bank'], f['counterparty_account'], f['counterparty_mfo'], f['counterparty_director'])}"
    )
    return content


# ═══════════════════════════════════════════════════════════
# IJARA SHARTNOMASI (FK 535-570)
# ═══════════════════════════════════════════════════════════

def generate_ijara(params: dict) -> str:
    """Ijara shartnomasi — FK 535-570.

    Muhim: 1 yildan ortiq muddatga tuzilgan ijara shartnomasi
    davlat ro'yxatiga olinishi SHART (FK 541-modda).
    """
    f = _common_fields(params)
    object_type = params.get("object_type", "noturar binolar")
    object_description = params.get("object_description", f["description"])
    object_area = params.get("object_area", BLANK)
    object_address = params.get("object_address", BLANK)
    cadastral_number = params.get("cadastral_number", "")
    rent_amount = params.get("rent_amount", f["amount"])
    rent_period = params.get("rent_period", "oylik")  # oylik, choraklik, yillik
    utilities_included = params.get("utilities_included", False)
    valid_from = params.get("valid_from", f["date"])
    valid_until = params.get("valid_until", BLANK)
    purpose = params.get("purpose", "ofis maqsadida foydalanish")

    amount_s = _amount_str(rent_amount)

    # 1 yildan ortiq — davlat ro'yxati kerak
    registration_note = ""
    if valid_until and valid_until != BLANK:
        registration_note = (
            "\nDIQQAT: Agar shartnoma muddati 1 yildan ortiq bo'lsa, "
            "FK 541-moddaga asosan davlat ro'yxatidan o'tkazilishi shart. "
            "Ro'yxatdan o'tmagan shartnoma uchinchi shaxslar uchun kuchga ega emas.\n"
        )

    # Kommunal to'lovlar
    if utilities_included:
        utility_text = "Ijara haqi kommunal xizmatlar (elektr, suv, issiqlik, gaz) bilan birga."
    else:
        utility_text = (
            "Kommunal xizmatlar (elektr, suv, issiqlik, gaz) alohida — "
            "Ijarachi haqiqiy iste'mol bo'yicha to'laydi."
        )

    content = (
        f"IJARA SHARTNOMASI No {f['number']}\n"
        f"(O'zR Fuqarolik Kodeksining 535-570-moddalari asosida)\n\n"
        f"{f['city']} shahri{' ' * 40}{f['date']}\n\n"
        f"{f['company']} (keyingi o'rinlarda \"Ijara beruvchi\" deb yuritiladi), "
        f"STIR {f['inn'] or BLANK}, rahbar {f['director'] or BLANK} shaxsida, "
        f"Ustav asosida faoliyat yurituvchi, bir tomondan, va\n\n"
        f"{f['counterparty']} (keyingi o'rinlarda \"Ijarachi\" deb yuritiladi), "
        f"STIR {f['counterparty_inn'] or BLANK}, rahbar {f['counterparty_director'] or BLANK} shaxsida, "
        f"Ustav asosida faoliyat yurituvchi, ikkinchi tomondan,\n\n"
        f"O'zR FK 535-moddasi asosida quyidagi shartnomani tuzdilar:\n\n"
        f"{registration_note}"
        #
        f"1. SHARTNOMA PREDMETI (FK 537)\n\n"
        f"1.1. Ijara beruvchi o'ziga tegishli quyidagi mol-mulkni (keyingi o'rinlarda "
        f"\"Ijara ob'ekti\" deb yuritiladi) Ijarachiga vaqtincha egalik qilish "
        f"va foydalanish uchun topshiradi:\n\n"
        f"  Ob'ekt turi: {object_type}\n"
        f"  Tavsifi: {object_description or BLANK}\n"
        f"  Maydoni: {object_area} kv.m\n"
        f"  Manzili: {object_address}\n"
    )
    if cadastral_number:
        content += f"  Kadastr raqami: {cadastral_number}\n"

    content += (
        f"\n1.2. Foydalanish maqsadi: {purpose}.\n"
        f"1.3. Ob'ekt qabul-topshirish dalolatnomasi bilan topshiriladi.\n\n"
        #
        f"2. IJARA MUDDATI (FK 540-541)\n\n"
        f"2.1. Ijara muddati: {valid_from} dan {valid_until} gacha.\n"
        f"2.2. Shartnoma muddati tugagandan so'ng, Ijarachining "
        f"shartnomani yangi muddatga tuzishda ustuvor huquqi bor (FK 548).\n"
        f"2.3. Shartnomani muddatidan oldin bekor qilish uchun kamida "
        f"30 kun oldin yozma ogohlantirish.\n\n"
        #
        f"3. IJARA HAQI VA TO'LOV TARTIBI (FK 544)\n\n"
        f"3.1. Ijara haqi: {amount_s} so'm ({rent_period}).\n"
        f"3.2. {utility_text}\n"
        f"3.3. To'lov muddati: har oyning 10-sanasigacha.\n"
        f"3.4. To'lov bank o'tkazmasi orqali amalga oshiriladi.\n"
        f"3.5. Ijara haqi yiliga 1 martadan ko'p o'zgartirilishi mumkin emas (FK 544).\n\n"
        #
        f"4. TOMONLARNING MAJBURIYATLARI\n\n"
        f"4.1. Ijara beruvchi:\n"
        f"  a) ob'ektni shartnomaga mos holatda topshirish (FK 539);\n"
        f"  b) kapital ta'mirni o'z hisobidan amalga oshirish (FK 545);\n"
        f"  c) Ijarachining ob'ektdan foydalanishiga to'sqinlik qilmaslik;\n"
        f"  d) ob'ektdagi uchinchi shaxslar huquqlari haqida xabar berish.\n\n"
        f"4.2. Ijarachi:\n"
        f"  a) ob'ektdan maqsadga muvofiq foydalanish;\n"
        f"  b) ijara haqini o'z vaqtida to'lash;\n"
        f"  c) joriy ta'mirni o'z hisobidan amalga oshirish (FK 546);\n"
        f"  d) ob'ektni qaytarish paytida dastlabki holatda (normal eskirish hisobga olingan holda) topshirish;\n"
        f"  e) Ijara beruvchining yozma roziligisiz sub'ijara bermaslik.\n\n"
        #
        f"5. OB'EKTNI QAYTARISH (FK 556)\n\n"
        f"5.1. Shartnoma muddati tugaganda yoki bekor qilinganda "
        f"Ijarachi ob'ektni qabul-topshirish dalolatnomasi bilan qaytaradi.\n"
        f"5.2. Qaytarish muddati: shartnoma muddati tugagan kundan boshlab 5 kun ichida.\n"
        f"5.3. Ob'ektga etkazilgan zarar Ijarachi tomonidan qoplanadi.\n\n"
        #
        f"6. JAVOBGARLIK\n\n"
        f"6.1. Ijara haqi kechiktirilsa — har bir kun uchun kechiktirilgan "
        f"summa ning 0,04% miqdorida penya.\n"
        f"6.2. Ob'ektni o'z vaqtida topshirmasa — Ijara beruvchi har bir kechiktirilgan "
        f"kun uchun kunlik ijara haqining 2 barobari miqdorida to'laydi.\n"
        f"6.3. Ijara beruvchi 2 oy ketma-ket to'lanmagan ijara haqi uchun "
        f"shartnomani sudda bekor qilishni talab qilishi mumkin (FK 554).\n\n"
        #
        f"7. FORS-MAJOR\n\n"
        f"7.1. Tabiiy ofatlar, urush, davlat qarorlari — fors-major hisoblanadi.\n"
        f"7.2. Fors-major davomida ijara haqi to'xtatiladi.\n\n"
        #
        f"8. NIZOLARNI HAL QILISH\n\n"
        f"8.1. Nizolar muzokaralar yo'li bilan hal qilinadi.\n"
        f"8.2. Sudgacha talabnoma (10 kun javob) majburiy.\n"
        f"8.3. Nizo iqtisodiy sudda ko'riladi.\n\n"
        #
        f"9. YAKUNIY QOIDALAR\n\n"
        f"9.1. Shartnomaga o'zgartirish faqat yozma kelishuv bilan.\n"
        f"9.2. Shartnoma 2 nusxada tuzilgan.\n"
        f"9.3. Shartnomaga ilova: ob'ektning qabul-topshirish dalolatnomasi.\n\n"
        #
        f"TOMONLAR REKVIZITLARI VA IMZOLARI\n\n"
        f"Ijara beruvchi:\n{_rekvizit(f['company'], f['inn'], f['address'], f['bank'], f['account'], f['mfo'], f['director'])}\n\n"
        f"Ijarachi:\n{_rekvizit(f['counterparty'], f['counterparty_inn'], f['counterparty_address'], f['counterparty_bank'], f['counterparty_account'], f['counterparty_mfo'], f['counterparty_director'])}"
    )
    return content


# ═══════════════════════════════════════════════════════════
# MEHNAT SHARTNOMASI (MK 103-132, yangi kodeks 2023)
# ═══════════════════════════════════════════════════════════

def generate_mehnat(params: dict) -> str:
    """Mehnat shartnomasi — MK 103-132 (yangi kodeks, 2023).

    Muhim: 2020 yildan MEHNAT.UZ da ro'yxatdan o'tishi SHART.
    Yangi MK (O'RQ-798, 28.10.2022, kuchga kirgan 30.04.2023).
    Eski MK 72-85 → yangi MK 103-132 ga o'tgan.
    MK 107: majburiy rekvizitlar (PINFL, INN, INPS).
    """
    f = _common_fields(params)
    employee_name = params.get("employee_name", params.get("counterparty_director", ""))
    passport = params.get("passport", BLANK)
    position = params.get("position", BLANK)
    department = params.get("department", "")
    salary = params.get("salary", f["amount"])
    salary_type = params.get("salary_type", "oylik")  # oylik, kunlik, ishbay
    work_schedule = params.get("work_schedule", "09:00 — 18:00, dushanba — juma")
    probation_months = params.get("probation_months", 0)
    contract_type = params.get("contract_type", "muddatsiz")  # muddatsiz, muddatli
    valid_until = params.get("valid_until", BLANK)
    vacation_days = params.get("vacation_days", 15)
    start_date = params.get("start_date", f["date"])
    pinfl = params.get("pinfl", BLANK)
    employee_inn = params.get("employee_inn", BLANK)

    salary_s = _amount_str(salary)

    # Sinov muddati (MK 114)
    probation_text = ""
    if probation_months:
        if probation_months > 3:
            probation_months = 3  # MK 114: max 3 oy
        probation_text = (
            f"3.1. Sinov muddati: {probation_months} oy (MK 114-modda, maksimal 3 oy).\n"
            f"3.2. Sinov muddati davomida xodimga MK ning barcha normalari to'liq tatbiq etiladi.\n"
            f"3.3. Sinov muddati davomida har qaysi tomon 3 kun oldin "
            f"ogohlantirish bilan shartnomani bekor qilishi mumkin.\n\n"
        )

    # Shartnoma turi
    if contract_type == "muddatli":
        type_text = (
            f"Muddatli mehnat shartnomasi — {start_date} dan {valid_until} gacha.\n"
            f"MK 106-moddaga asosan muddatli shartnoma faqat qonunda belgilangan "
            f"hollardagina tuziladi."
        )
    else:
        type_text = "Muddatsiz mehnat shartnomasi."

    content = (
        f"MEHNAT SHARTNOMASI No {f['number']}\n"
        f"(O'zR Mehnat Kodeksining 103-132-moddalari asosida)\n\n"
        f"{f['city']} shahri{' ' * 40}{f['date']}\n\n"
        f"DIQQAT: Mazkur shartnoma my.mehnat.uz portalida elektron ro'yxatdan "
        f"o'tkazilishi shart (2020 yildan majburiy, ERI bilan).\n\n"
        f"{f['company']} (keyingi o'rinlarda \"Ish beruvchi\" deb yuritiladi), "
        f"STIR {f['inn'] or BLANK}, rahbar {f['director'] or BLANK} shaxsida, "
        f"Ustav asosida faoliyat yurituvchi, bir tomondan, va\n\n"
        f"fuqaro {employee_name or BLANK} (keyingi o'rinlarda \"Xodim\" deb yuritiladi), "
        f"pasport {passport}, ikkinchi tomondan,\n\n"
        f"O'zR MK 103-moddasi asosida quyidagi mehnat shartnomani tuzdilar:\n\n"
        #
        f"1. UMUMIY QOIDALAR\n\n"
        f"1.1. {type_text}\n"
        f"1.2. Ishga qabul qilish sanasi: {start_date}.\n"
        f"1.3. Ish joyi: {f['company']}, {f['address'] or BLANK}.\n\n"
        #
        f"2. XODIMNING LAVOZIMI VA VAZIFALARI (MK 104)\n\n"
        f"2.1. Lavozimi: {position}.\n"
    )
    if department:
        content += f"2.2. Bo'lim: {department}.\n"

    content += (
        f"2.3. Asosiy vazifalari:\n"
        f"  a) lavozim yo'riqnomasi bo'yicha ishlarni sifatli bajarish;\n"
        f"  b) ichki mehnat tartib qoidalariga rioya qilish;\n"
        f"  c) mehnat xavfsizligi qoidalariga rioya qilish;\n"
        f"  d) Ish beruvchining mol-mulkiga ehtiyotkorlik bilan munosabatda bo'lish.\n\n"
    )

    if probation_text:
        content += f"3. SINOV MUDDATI (MK 114)\n\n{probation_text}"
        next_section = 4
    else:
        next_section = 3

    content += (
        f"{next_section}. ISH VAQTI VA DAM OLISH (MK 115-132)\n\n"
        f"{next_section}.1. Ish vaqti: {work_schedule}.\n"
        f"{next_section}.2. Kunlik ish vaqti: 8 soat (haftalik 40 soat).\n"
        f"{next_section}.3. Tushlik tanaffusi: 13:00 — 14:00 (1 soat, ish vaqtiga kirmaydi).\n"
        f"{next_section}.4. Dam olish kunlari: shanba, yakshanba.\n"
        f"{next_section}.5. Yillik mehnat ta'tili: {vacation_days} ish kuni (MK 134).\n"
        f"{next_section}.6. Davlat bayramlari — qo'shimcha dam olish kunlari.\n\n"
    )
    next_section += 1

    content += (
        f"{next_section}. MEHNAT HAQI (MK 153-165)\n\n"
        f"{next_section}.1. Mehnat haqi: {salary_s} so'm ({salary_type}).\n"
        f"{next_section}.2. Mehnat haqi har oyning 10 va 25-sanalarida ikki qismda to'lanadi.\n"
        f"{next_section}.3. Mehnat haqidan ushlab qolinadigan soliq va ajratmalar:\n"
        f"  - Jismoniy shaxslardan olinadigan daromad solig'i (JSHDS): 12%\n"
        f"  - INPS (ijtimoiy nafaqa): 1% xodim hisobidan\n"
        f"{next_section}.4. Ortiqcha ish vaqti uchun MK 157-moddaga muvofiq to'lanadi.\n"
        f"{next_section}.5. Mehnat haqi O'zR amaldagi eng kam ish haqidan kam bo'lmasligi kerak.\n\n"
    )
    next_section += 1

    content += (
        f"{next_section}. TOMONLARNING HUQUQ VA MAJBURIYATLARI\n\n"
        f"{next_section}.1. Xodimning huquqlari (MK 75):\n"
        f"  a) mehnat sharoitlari MK talablariga mos bo'lishi;\n"
        f"  b) mehnat haqini o'z vaqtida va to'liq olish;\n"
        f"  c) yillik mehnat ta'tilidan foydalanish;\n"
        f"  d) kasbiy tayyorgarlikdan o'tish;\n"
        f"  e) mehnat nizolarini qonunda belgilangan tartibda hal qilish.\n\n"
        f"{next_section}.2. Ish beruvchining majburiyatlari:\n"
        f"  a) mehnat sharoitlarini yaratish va mehnat xavfsizligini ta'minlash;\n"
        f"  b) mehnat haqini o'z vaqtida to'lash;\n"
        f"  c) ijtimoiy sug'urta to'lovlarini amalga oshirish;\n"
        f"  d) mehnat daftarchasiga yozuv kiritish;\n"
        f"  e) MK da ko'rsatilgan kafolatlarni ta'minlash.\n\n"
    )
    next_section += 1

    content += (
        f"{next_section}. MODDIY JAVOBGARLIK (MK 183-193)\n\n"
        f"{next_section}.1. Xodim Ish beruvchiga etkazilgan zararni MK 183-moddaga "
        f"muvofiq qoplaydi.\n"
        f"{next_section}.2. To'liq moddiy javobgarlik faqat MK 186-moddada belgilangan "
        f"hollarda yuzaga keladi.\n\n"
    )
    next_section += 1

    content += (
        f"{next_section}. SHARTNOMANI BEKOR QILISH (MK 97-107)\n\n"
        f"{next_section}.1. Xodim tashabbusi bilan — kamida 2 hafta oldin "
        f"yozma ariza berish (MK 99).\n"
        f"{next_section}.2. Ish beruvchi tashabbusi bilan — faqat MK 100-moddada "
        f"ko'rsatilgan asoslarda (shtit qisqartirish, malakasizlik, intizomiy jazo va h.k.).\n"
        f"{next_section}.3. Tomonlarning kelishuvi bilan — har qachon (MK 97).\n"
        f"{next_section}.4. Shartnoma muddati tugashi bilan — muddatli shartnomalar uchun.\n\n"
    )
    next_section += 1

    content += (
        f"{next_section}. BOSHQA SHARTLAR\n\n"
        f"{next_section}.1. Xodim tijorat sirini saqlash majburiyatini oladi.\n"
        f"{next_section}.2. Shartnomaga o'zgartirish faqat tomonlarning yozma "
        f"kelishuviga asosan kiritiladi.\n"
        f"{next_section}.3. Shartnoma 2 nusxada tuzilgan, har bir tomon uchun bittadan.\n"
        f"{next_section}.4. Shartnomada ko'rsatilmagan masalalar O'zR MK ga muvofiq hal qilinadi.\n\n"
        #
        f"TOMONLARNING REKVIZITLARI VA IMZOLARI\n\n"
        f"Ish beruvchi:\n"
        f"{f['company']}\n"
        f"STIR: {f['inn'] or BLANK}\n"
        f"Manzil: {f['address'] or BLANK}\n"
        f"Rahbar: {f['director'] or BLANK}\n\n"
        f"_______________ / {f['director'] or BLANK} /\n"
        f"M.O.\n\n"
        f"Xodim:\n"
        f"F.I.Sh.: {employee_name or BLANK}\n"
        f"Pasport: {passport}\n"
        f"PINFL: {pinfl}\n"
        f"INN: {employee_inn}\n"
        f"Manzil: {f['counterparty_address'] or BLANK}\n\n"
        f"_______________ / {employee_name or BLANK} /\n\n"
        f"Shartnoma nusxasini oldim: _______________ / {employee_name or BLANK} /\n"
        f"Sana: {f['date']}"
    )
    return content


# ═══════════════════════════════════════════════════════════
# SOLISHTIRMA DALOLATNOMA (Reconciliation Act)
# ═══════════════════════════════════════════════════════════

def generate_solishtirma(params: dict) -> str:
    """Solishtirma dalolatnoma — kvartollik qarz tekshirish.

    Muhim: 3 yillik da'vo muddatini qayta boshlaydi (FK 159).
    """
    f = _common_fields(params)
    period_from = params.get("period_from", BLANK)
    period_to = params.get("period_to", BLANK)
    opening_balance = params.get("opening_balance", 0)
    opening_balance_side = params.get("opening_balance_side", "")  # debit yoki kredit
    operations = params.get("operations", [])
    # operations: [{date, description, debit, credit}]

    # Operatsiyalar jadvali va yakuniy qoldiq hisoblash
    total_debit = 0
    total_credit = 0
    ops_text = ""

    if operations:
        ops_text = "Sana | Hujjat/Operatsiya | Debet (so'm) | Kredit (so'm)\n"
        ops_text += "=" * 70 + "\n"
        for op in operations:
            op_date = op.get("date", "")
            op_desc = op.get("description", "")
            op_debit = op.get("debit", 0)
            op_credit = op.get("credit", 0)
            total_debit += op_debit
            total_credit += op_credit
            d_str = f"{op_debit:,.0f}" if op_debit else ""
            c_str = f"{op_credit:,.0f}" if op_credit else ""
            ops_text += f"{op_date} | {op_desc} | {d_str} | {c_str}\n"
        ops_text += "=" * 70 + "\n"
        ops_text += f"Jami: | | {total_debit:,.0f} | {total_credit:,.0f}\n"

    # Yakuniy qoldiq
    if opening_balance_side == "debit":
        closing = opening_balance + total_debit - total_credit
    elif opening_balance_side == "kredit":
        closing = opening_balance + total_credit - total_debit
    else:
        closing = total_debit - total_credit

    if closing > 0:
        closing_text = f"{abs(closing):,.0f} so'm — {f['counterparty']} qarzdor"
        closing_side = "debit"
    elif closing < 0:
        closing_text = f"{abs(closing):,.0f} so'm — {f['company']} qarzdor"
        closing_side = "kredit"
    else:
        closing_text = "0 so'm — qarz yo'q"
        closing_side = "teng"

    # Boshlang'ich qoldiq matni
    if opening_balance and opening_balance_side:
        ob_text = (
            f"Boshlang'ich qoldiq ({period_from} holatiga): "
            f"{opening_balance:,.0f} so'm ({opening_balance_side})"
        )
    else:
        ob_text = f"Boshlang'ich qoldiq ({period_from} holatiga): 0 so'm"

    content = (
        f"O'ZARO HISOB-KITOBLARNI SOLISHTIRISH DALOLATNOMASI\n\n"
        f"Sana: {f['date']}\n"
        f"Davr: {period_from} dan {period_to} gacha\n\n"
        f"Biz, quyida imzo chekuvchilar:\n\n"
        f"{f['company']} (STIR {f['inn'] or BLANK}) nomidan — "
        f"{f['director'] or BLANK},\n"
        f"{f['counterparty']} (STIR {f['counterparty_inn'] or BLANK}) nomidan — "
        f"{f['counterparty_director'] or BLANK},\n\n"
        f"mazkur dalolatnomani {period_from} dan {period_to} gacha bo'lgan davr uchun "
        f"o'zaro hisob-kitoblarni solishtirib tuzdik.\n\n"
        #
        f"{ob_text}\n\n"
    )

    if ops_text:
        content += f"OPERATSIYALAR:\n\n{ops_text}\n"

    content += (
        f"YAKUNIY QOLDIQ ({period_to} holatiga): {closing_text}\n\n"
        f"{'=' * 60}\n\n"
        f"Mazkur dalolatnomaga asosan:\n"
    )

    if closing_side == "debit":
        content += (
            f"- {f['counterparty']} ning {f['company']} ga qarzi "
            f"{abs(closing):,.0f} so'm ni tashkil etadi.\n"
        )
    elif closing_side == "kredit":
        content += (
            f"- {f['company']} ning {f['counterparty']} ga qarzi "
            f"{abs(closing):,.0f} so'm ni tashkil etadi.\n"
        )
    else:
        content += "- Tomonlar o'rtasida qarz yo'q.\n"

    content += (
        f"\nEslatma: Ushbu dalolatnoma imzolangandan so'ng, FK 159-moddaga asosan "
        f"3 yillik da'vo muddati qaytadan boshlanadi.\n\n"
        f"Agar Siz mazkur dalolatnomaga 10 kun ichida yozma e'tiroz bildirmasangiz, "
        f"dalolatnoma tomonlar tomonidan tan olingan hisoblanadi.\n\n"
        #
        f"{f['company']}:\n"
        f"Rahbar: _______________ / {f['director'] or BLANK} /\n"
        f"Bosh hisobchi: _______________ / {BLANK} /\n"
        f"M.O.\n\n"
        f"{f['counterparty']}:\n"
        f"Rahbar: _______________ / {f['counterparty_director'] or BLANK} /\n"
        f"Bosh hisobchi: _______________ / {BLANK} /\n"
        f"M.O."
    )
    return content


# ═══════════════════════════════════════════════════════════
# TIJORAT TAKLIFI (FK 365-369 — Oferta)
# ═══════════════════════════════════════════════════════════

def generate_tijorat_taklifi(params: dict) -> str:
    """Tijorat taklifi — FK 365-369 asosida.

    FK 365: Oferta — shartnoma tuzish haqidagi taklifnoma.
    FK 367: Ofertani qabul qilish (aksept).
    FK 369: Oferta muddati va qaytarilmasligi.
    """
    f = _common_fields(params)
    items = params.get("items", [])
    valid_days = params.get("valid_days", 30)
    delivery_terms = params.get("delivery_terms", "")
    payment_terms = params.get("payment_terms", "")
    special_conditions = params.get("special_conditions", "")
    contact_person = params.get("contact_person", "")
    contact_phone = params.get("contact_phone", "")
    contact_email = params.get("contact_email", "")

    amount = f["amount"]
    amount_s = _amount_str(amount)

    # Tovarlar/xizmatlar jadvali
    items_text = ""
    if items:
        items_text = "No | Nomi | O'lchov | Soni | Narx (so'm) | Jami (so'm)\n"
        items_text += "=" * 70 + "\n"
        total = 0
        for i, item in enumerate(items, 1):
            name = item.get("name", "")
            qty = item.get("quantity", 1)
            unit = item.get("unit", "dona")
            price = item.get("price", 0)
            summa = qty * price
            total += summa
            items_text += f"{i}. {name} | {unit} | {qty} | {price:,.0f} | {summa:,.0f}\n"
        items_text += "=" * 70 + "\n"
        vat = round(total * 12 / 100)
        items_text += f"Jami (QQSsiz): {total:,.0f} so'm\n"
        items_text += f"QQS (12%): {vat:,.0f} so'm\n"
        items_text += f"JAMI (QQS bilan): {total + vat:,.0f} so'm\n"
        if amount == 0:
            amount = total
            amount_s = _amount_str(amount)

    content = (
        f"TIJORAT TAKLIFI (OFERTA)\n"
        f"No {f['number']} sanasi {f['date']}\n\n"
        f"Kimga: {f['counterparty']}\n"
    )
    if f['counterparty_director']:
        content += f"Hurmatli {f['counterparty_director']}!\n\n"
    else:
        content += f"Hurmatli rahbar!\n\n"

    content += (
        f"{f['company']} (STIR {f['inn'] or BLANK}) Sizga quyidagi "
        f"tovar/xizmatlarni taklif etadi:\n\n"
    )

    if items_text:
        content += f"TAKLIF ETILAYOTGAN TOVAR/XIZMATLAR:\n\n{items_text}\n"
    else:
        content += (
            f"TAKLIF:\n\n"
            f"{f['description'] or BLANK}\n\n"
            f"Umumiy qiymat: {amount_s} so'm (QQS alohida).\n\n"
        )

    content += f"SHARTLAR:\n\n"

    if delivery_terms:
        content += f"Yetkazib berish: {delivery_terms}\n"
    else:
        content += "Yetkazib berish: shartnoma imzolanganidan keyin 10 ish kuni ichida.\n"

    if payment_terms:
        content += f"To'lov: {payment_terms}\n"
    else:
        content += "To'lov: bank o'tkazmasi, yetkazib berganidan keyin 5 bank kuni ichida.\n"

    content += (
        f"Kafolat: ishlab chiqaruvchi kafolati.\n"
        f"QQS: 12% (alohida).\n"
    )

    if special_conditions:
        content += f"\nQo'shimcha shartlar: {special_conditions}\n"

    content += (
        f"\nTAKLIF AMAL QILISH MUDDATI:\n\n"
        f"Mazkur taklif {f['date']} dan boshlab {valid_days} kalendar kun "
        f"davomida amal qiladi (FK 369-modda).\n"
        f"Muddati o'tgandan keyin taklif o'z kuchini yo'qotadi.\n\n"
        f"FK 365-moddaga asosan, agar Siz mazkur taklifni belgilangan muddat ichida "
        f"yozma qabul qilsangiz (aksept), bu shartnoma tuzishga asos bo'ladi.\n\n"
        f"HAMKORLIK UCHUN BOG'LANISH:\n\n"
        f"{f['company']}\n"
    )
    if contact_person:
        content += f"Mas'ul shaxs: {contact_person}\n"
    if contact_phone:
        content += f"Telefon: {contact_phone}\n"
    if contact_email:
        content += f"Email: {contact_email}\n"
    content += (
        f"Manzil: {f['address'] or BLANK}\n\n"
        f"Hurmat bilan,\n\n"
        f"Rahbar: _______________ / {f['director'] or BLANK} /\n"
        f"M.O."
    )
    return content


# ═══════════════════════════════════════════════════════════
# TIER 2: XIZMAT KO'RSATISH SHARTNOMASI (FK 703-714)
# ═══════════════════════════════════════════════════════════

def generate_xizmat(params: dict) -> str:
    """Xizmat ko'rsatish shartnomasi — FK 703-714.

    Eng ko'p tuziladigan shartnoma turi.
    Pudratdan farqi: natija emas, jarayon muhim.
    """
    f = _common_fields(params)
    service_list = params.get("service_list", f["description"])
    service_period = params.get("service_period", BLANK)
    payment_schedule = params.get("payment_schedule", "oylik")
    valid_until = params.get("valid_until", BLANK)
    report_required = params.get("report_required", True)

    amount = f["amount"]
    amount_s = _amount_str(amount)

    content = (
        f"XIZMAT KO'RSATISH SHARTNOMASI No {f['number']}\n"
        f"(O'zR Fuqarolik Kodeksining 703-714-moddalari asosida)\n\n"
        f"{f['city']} shahri{' ' * 40}{f['date']}\n\n"
        f"{f['company']} (keyingi o'rinlarda \"Buyurtmachi\" deb yuritiladi), "
        f"STIR {f['inn'] or BLANK}, rahbar {f['director'] or BLANK} shaxsida, "
        f"Ustav asosida faoliyat yurituvchi, bir tomondan, va\n\n"
        f"{f['counterparty']} (keyingi o'rinlarda \"Ijrochi\" deb yuritiladi), "
        f"STIR {f['counterparty_inn'] or BLANK}, rahbar {f['counterparty_director'] or BLANK} shaxsida, "
        f"Ustav asosida faoliyat yurituvchi, ikkinchi tomondan,\n\n"
        f"O'zR FK 703-moddasi asosida quyidagi shartnomani tuzdilar:\n\n"
        #
        f"1. SHARTNOMA PREDMETI\n\n"
        f"1.1. Ijrochi Buyurtmachiga quyidagi xizmatlarni ko'rsatishni, "
        f"Buyurtmachi esa ularni qabul qilib to'lashni o'z zimmasiga oladi:\n\n"
        f"{service_list or BLANK}\n\n"
        f"1.2. Xizmat ko'rsatish davri: {service_period}.\n"
        f"1.3. Shartnomaning umumiy qiymati: {amount_s} so'm (QQS alohida).\n\n"
        #
        f"2. TOMONLARNING MAJBURIYATLARI\n\n"
        f"2.1. Ijrochi:\n"
        f"  a) xizmatlarni sifatli va belgilangan muddatda ko'rsatish;\n"
        f"  b) xizmat ko'rsatish jarayoni haqida Buyurtmachiga xabar berish;\n"
        f"  c) Buyurtmachining ko'rsatmalariga rioya qilish (FK 706);\n"
        f"  d) xizmat sifatini ta'minlash uchun zarur malaka va tajribaga ega bo'lish.\n\n"
        f"2.2. Buyurtmachi:\n"
        f"  a) xizmat ko'rsatish uchun zarur sharoit va ma'lumotlarni berish;\n"
        f"  b) ko'rsatilgan xizmatlarni o'z vaqtida qabul qilish;\n"
        f"  c) to'lovni belgilangan muddatda amalga oshirish.\n\n"
        #
        f"3. XIZMAT SIFATI\n\n"
        f"3.1. Xizmatlar O'zR amaldagi standartlari va shartnoma shartlariga mos ko'rsatiladi.\n"
        f"3.2. Sifatga oid da'vo — xizmat ko'rsatilgan kundan boshlab 10 kun ichida bildiriladi.\n\n"
        #
        f"4. NARX VA TO'LOV TARTIBI\n\n"
        f"4.1. Xizmat narxi: {amount_s} so'm ({payment_schedule}).\n"
        f"4.2. To'lov bank o'tkazmasi orqali amalga oshiriladi.\n"
    )

    if report_required:
        content += (
            f"4.3. To'lov uchun asos: ko'rsatilgan xizmatlar dalolatnomasi va hisob-faktura.\n\n"
            f"5. HISOBOT\n\n"
            f"5.1. Ijrochi har oy oxirida ko'rsatilgan xizmatlar dalolatnomasi taqdim etadi.\n"
            f"5.2. Buyurtmachi dalolatnomani 5 ish kuni ichida ko'rib chiqib, "
            f"imzolaydi yoki asosli e'tiroz bildiradi.\n\n"
        )
        next_s = 6
    else:
        content += "\n"
        next_s = 5

    content += (
        f"{next_s}. SHARTNOMANI BEKOR QILISH (FK 709)\n\n"
        f"{next_s}.1. Buyurtmachi istalgan vaqtda shartnomani bekor qilishi mumkin, "
        f"bunda Ijrochiga haqiqatda ko'rsatilgan xizmatlar uchun to'laydi (FK 709).\n"
        f"{next_s}.2. Ijrochi Buyurtmachiga etkazilgan zararlarni to'liq qoplagan "
        f"holda shartnomani bekor qilishi mumkin.\n"
        f"{next_s}.3. Bekor qilish uchun kamida 15 kun oldin yozma ogohlantirish.\n\n"
    )
    next_s += 1
    content += (
        f"{next_s}. JAVOBGARLIK\n\n"
        f"{next_s}.1. Xizmat kechiktirilsa — har kun uchun shartnoma summasining "
        f"0,5% miqdorida penya, lekin 50% dan oshmasligi kerak.\n"
        f"{next_s}.2. To'lov kechiktirilsa — qarz summasining 0,04% kuniga.\n\n"
    )
    next_s += 1
    content += (
        f"{next_s}. NIZOLARNI HAL QILISH\n\n"
        f"{next_s}.1. Nizolar muzokaralar yo'li bilan hal qilinadi.\n"
        f"{next_s}.2. Sudgacha talabnoma (10 kun javob) majburiy.\n"
        f"{next_s}.3. Nizo iqtisodiy sudda ko'riladi.\n\n"
    )
    next_s += 1
    content += (
        f"{next_s}. SHARTNOMA MUDDATI\n\n"
        f"{next_s}.1. Shartnoma imzolangan kundan {valid_until} gacha amal qiladi.\n"
        f"{next_s}.2. Shartnoma 2 nusxada tuzilgan.\n\n"
        f"TOMONLAR REKVIZITLARI VA IMZOLARI\n\n"
        f"Buyurtmachi:\n{_rekvizit(f['company'], f['inn'], f['address'], f['bank'], f['account'], f['mfo'], f['director'])}\n\n"
        f"Ijrochi:\n{_rekvizit(f['counterparty'], f['counterparty_inn'], f['counterparty_address'], f['counterparty_bank'], f['counterparty_account'], f['counterparty_mfo'], f['counterparty_director'])}"
    )
    return content


# ═══════════════════════════════════════════════════════════
# QABUL-TOPSHIRISH DALOLATNOMASI
# ═══════════════════════════════════════════════════════════

def generate_qabul_topshirish(params: dict) -> str:
    """Qabul-topshirish dalolatnomasi — tovar/xizmat/aktiv qabul qilish."""
    f = _common_fields(params)
    items = params.get("items", [])
    contract_ref = params.get("contract_number", BLANK)
    contract_date = params.get("contract_date", BLANK)
    quality_ok = params.get("quality_ok", True)
    remarks = params.get("remarks", "")

    amount = f["amount"]
    amount_s = _amount_str(amount)

    # Tovarlar jadvali
    items_text = ""
    total = 0
    if items:
        items_text = "No | Nomi | O'lchov | Soni | Narx (so'm) | Jami (so'm)\n"
        items_text += "=" * 70 + "\n"
        for i, item in enumerate(items, 1):
            name = item.get("name", "")
            qty = item.get("quantity", 1)
            unit = item.get("unit", "dona")
            price = item.get("price", 0)
            summa = qty * price
            total += summa
            items_text += f"{i}. {name} | {unit} | {qty} | {price:,.0f} | {summa:,.0f}\n"
        items_text += "=" * 70 + "\n"
        items_text += f"Jami: {total:,.0f} so'm\n"
        if amount == 0:
            amount = total
            amount_s = _amount_str(amount)

    content = (
        f"QABUL-TOPSHIRISH DALOLATNOMASI No {f['number']}\n\n"
        f"Sana: {f['date']}\n"
        f"Shartnoma asosi: No {contract_ref} sanasi {contract_date}\n\n"
        f"Topshiruvchi: {f['company']}, STIR {f['inn'] or BLANK}\n"
        f"Qabul qiluvchi: {f['counterparty']}, STIR {f['counterparty_inn'] or BLANK}\n\n"
    )

    if items_text:
        content += f"TOPSHIRILGAN TOVAR/XIZMATLAR:\n\n{items_text}\n"
    else:
        content += (
            f"TOPSHIRILGAN TOVAR/XIZMATLAR:\n\n"
            f"{f['description'] or BLANK}\n\n"
            f"Umumiy qiymat: {amount_s} so'm\n\n"
        )

    vat = round(amount * 12 / 100) if amount else 0
    content += (
        f"Qiymat (QQSsiz): {amount_s} so'm\n"
        f"QQS (12%): {vat:,.0f} so'm\n"
        f"Jami (QQS bilan): {amount + vat:,.0f} so'm\n\n"
    )

    if quality_ok:
        content += "Sifat va miqdor bo'yicha da'volar: YO'Q\n"
    else:
        content += f"Sifat va miqdor bo'yicha da'volar: BOR\n"
        if remarks:
            content += f"Izoh: {remarks}\n"

    content += (
        f"\nTovar/xizmatlar to'liq hajmda qabul qilindi.\n\n"
        f"Topshiruvchi:\n"
        f"{f['company']}\n"
        f"Rahbar: _______________ / {f['director'] or BLANK} /\n"
        f"M.O.\n\n"
        f"Qabul qiluvchi:\n"
        f"{f['counterparty']}\n"
        f"Rahbar: _______________ / {f['counterparty_director'] or BLANK} /\n"
        f"M.O."
    )
    return content


# ═══════════════════════════════════════════════════════════
# BUYRUQLAR — T-1, T-6, T-8 (VMQ 1297 yagona shakllar)
# ═══════════════════════════════════════════════════════════

def generate_buyruq_t1(params: dict) -> str:
    """T-1 Buyruq — ishga qabul qilish (VMQ 1297)."""
    f = _common_fields(params)
    employee_name = params.get("employee_name", "")
    position = params.get("position", BLANK)
    department = params.get("department", "")
    salary = params.get("salary", 0)
    start_date = params.get("start_date", f["date"])
    contract_type = params.get("contract_type", "muddatsiz")
    contract_number = params.get("contract_number", BLANK)
    contract_date = params.get("contract_date", f["date"])
    probation_months = params.get("probation_months", 0)
    work_type = params.get("work_type", "asosiy")  # asosiy, o'rindoshlik

    salary_s = _amount_str(salary)

    content = (
        f"{f['company']}\n"
        f"STIR: {f['inn'] or BLANK}\n\n"
        f"{'=' * 50}\n"
        f"BUYRUQ No {f['number']}\n"
        f"Ishga qabul qilish to'g'risida\n"
        f"(T-1 shakli, VMQ 1297)\n"
        f"{'=' * 50}\n\n"
        f"Sana: {f['date']}\n\n"
        f"Quyidagi fuqaroni ishga qabul qilish BUYURILSIN:\n\n"
        f"F.I.Sh.: {employee_name or BLANK}\n"
        f"Lavozim: {position}\n"
    )
    if department:
        content += f"Bo'lim: {department}\n"
    content += (
        f"Ish turi: {work_type}\n"
        f"Ishga kirish sanasi: {start_date}\n"
        f"Mehnat shartnomasi: No {contract_number} sanasi {contract_date}\n"
        f"Shartnoma turi: {contract_type}\n"
        f"Mehnat haqi (oylik): {salary_s} so'm\n"
    )
    if probation_months:
        content += f"Sinov muddati: {probation_months} oy\n"
    content += (
        f"\nAsos: Mehnat shartnomasi No {contract_number} sanasi {contract_date}\n\n"
        f"Rahbar: _______________ / {f['director'] or BLANK} /\n"
        f"M.O.\n\n"
        f"Buyruq bilan tanishdim:\n"
        f"_______________ / {employee_name or BLANK} /\n"
        f"Sana: {BLANK}"
    )
    return content


def generate_buyruq_t6(params: dict) -> str:
    """T-6 Buyruq — mehnat ta'tili berish (VMQ 1297)."""
    f = _common_fields(params)
    employee_name = params.get("employee_name", "")
    position = params.get("position", BLANK)
    department = params.get("department", "")
    leave_type = params.get("leave_type", "yillik")  # yillik, haqi saqlanmaydigan, o'quv
    leave_from = params.get("leave_from", BLANK)
    leave_to = params.get("leave_to", BLANK)
    leave_days = params.get("leave_days", 15)
    work_period = params.get("work_period", "")  # "01.01.2025 — 31.12.2025"

    leave_type_text = {
        "yillik": "Yillik asosiy mehnat ta'tili",
        "qoshimcha": "Qo'shimcha mehnat ta'tili",
        "haqi_saqlanmaydigan": "Mehnat haqi saqlanmaydigan ta'til",
        "oquv": "O'quv ta'tili",
        "tugish": "Homiladorlik va tug'ish ta'tili",
        "bola": "Bolani parvarish qilish ta'tili",
    }.get(leave_type, leave_type)

    content = (
        f"{f['company']}\n"
        f"STIR: {f['inn'] or BLANK}\n\n"
        f"{'=' * 50}\n"
        f"BUYRUQ No {f['number']}\n"
        f"Mehnat ta'tili berish to'g'risida\n"
        f"(T-6 shakli, VMQ 1297)\n"
        f"{'=' * 50}\n\n"
        f"Sana: {f['date']}\n\n"
        f"Quyidagi xodimga ta'til berish BUYURILSIN:\n\n"
        f"F.I.Sh.: {employee_name or BLANK}\n"
        f"Lavozim: {position}\n"
    )
    if department:
        content += f"Bo'lim: {department}\n"
    if work_period:
        content += f"Ish davri uchun: {work_period}\n"
    content += (
        f"\nTa'til turi: {leave_type_text}\n"
        f"Ta'til muddati: {leave_days} kalendar kun\n"
        f"Boshlanishi: {leave_from}\n"
        f"Tugashi: {leave_to}\n\n"
        f"Rahbar: _______________ / {f['director'] or BLANK} /\n"
        f"M.O.\n\n"
        f"Buyruq bilan tanishdim:\n"
        f"_______________ / {employee_name or BLANK} /\n"
        f"Sana: {BLANK}"
    )
    return content


def generate_buyruq_t8(params: dict) -> str:
    """T-8 Buyruq — mehnat shartnomasini bekor qilish (VMQ 1297)."""
    f = _common_fields(params)
    employee_name = params.get("employee_name", "")
    position = params.get("position", BLANK)
    department = params.get("department", "")
    dismissal_date = params.get("dismissal_date", f["date"])
    dismissal_reason = params.get("dismissal_reason", "")
    dismissal_article = params.get("dismissal_article", "")
    contract_number = params.get("contract_number", BLANK)
    contract_date = params.get("contract_date", BLANK)
    basis_document = params.get("basis_document", "")

    # Standart sabablar
    reason_texts = {
        "oz_xohishi": "Xodimning o'z xohishiga ko'ra (MK 99-modda)",
        "kelishuv": "Tomonlarning o'zaro kelishuviga asosan (MK 97-modda)",
        "muddat": "Mehnat shartnomasi muddatining tugashi (MK 105-modda)",
        "qisqartirish": "Shtat qisqartirish (MK 100-modda, 1-band)",
        "malakasizlik": "Malaka yetishmasligi (MK 100-modda, 2-band)",
        "intizom": "Intizomiy jazo (MK 100-modda, 3-band)",
    }

    if dismissal_reason in reason_texts:
        reason_text = reason_texts[dismissal_reason]
    elif dismissal_reason:
        reason_text = dismissal_reason
        if dismissal_article:
            reason_text += f" ({dismissal_article})"
    else:
        reason_text = BLANK

    content = (
        f"{f['company']}\n"
        f"STIR: {f['inn'] or BLANK}\n\n"
        f"{'=' * 50}\n"
        f"BUYRUQ No {f['number']}\n"
        f"Mehnat shartnomasini bekor qilish to'g'risida\n"
        f"(T-8 shakli, VMQ 1297)\n"
        f"{'=' * 50}\n\n"
        f"Sana: {f['date']}\n\n"
        f"Quyidagi xodim bilan mehnat shartnomasini bekor qilish BUYURILSIN:\n\n"
        f"F.I.Sh.: {employee_name or BLANK}\n"
        f"Lavozim: {position}\n"
    )
    if department:
        content += f"Bo'lim: {department}\n"
    content += (
        f"Mehnat shartnomasi: No {contract_number} sanasi {contract_date}\n"
        f"Ishdan bo'shatish sanasi: {dismissal_date}\n\n"
        f"Bo'shatish sababi: {reason_text}\n"
    )
    if basis_document:
        content += f"Asos: {basis_document}\n"
    content += (
        f"\nBosh hisobchiga: hisob-kitobni amalga oshirish topshirilsin.\n"
        f"Kadrlar bo'limiga: mehnat daftarchasiga tegishli yozuv kiritish topshirilsin.\n\n"
        f"Rahbar: _______________ / {f['director'] or BLANK} /\n"
        f"M.O.\n\n"
        f"Buyruq bilan tanishdim:\n"
        f"_______________ / {employee_name or BLANK} /\n"
        f"Sana: {BLANK}"
    )
    return content


# ═══════════════════════════════════════════════════════════
# PUDRAT SHARTNOMASI (FK 631-670)
# ═══════════════════════════════════════════════════════════

def generate_pudrat(params: dict) -> str:
    """Pudrat shartnomasi — FK 631-670.

    Xizmatdan farqi: natija muhim (bino, ta'mir, ishlab chiqarish).
    Pudratchi o'z xavfi va materiallari bilan ishlaydi.
    """
    f = _common_fields(params)
    work_description = params.get("work_description", f["description"])
    work_start = params.get("work_start", f["date"])
    work_end = params.get("work_end", BLANK)
    materials_by = params.get("materials_by", "pudratchi")  # pudratchi, buyurtmachi, aralash
    valid_until = params.get("valid_until", BLANK)
    warranty_months = params.get("warranty_months", 12)
    prepay_pct = params.get("prepay_pct", 0)

    amount = f["amount"]
    amount_s = _amount_str(amount)

    materials_text = {
        "pudratchi": "Pudratchi o'z materiallari va asbob-uskunalari bilan bajaradi (FK 635).",
        "buyurtmachi": "Buyurtmachi materiallari bilan bajariladi. Pudratchi materiallarni tejamkorlik bilan sarflaydi va hisobot beradi (FK 636).",
        "aralash": "Materiallar aralash: asosiy materiallar Buyurtmachi tomonidan, qo'shimcha materiallar Pudratchi tomonidan ta'minlanadi.",
    }.get(materials_by, materials_by)

    content = (
        f"PUDRAT SHARTNOMASI No {f['number']}\n"
        f"(O'zR Fuqarolik Kodeksining 631-670-moddalari asosida)\n\n"
        f"{f['city']} shahri{' ' * 40}{f['date']}\n\n"
        f"{f['company']} (keyingi o'rinlarda \"Buyurtmachi\" deb yuritiladi), "
        f"STIR {f['inn'] or BLANK}, rahbar {f['director'] or BLANK} shaxsida, "
        f"Ustav asosida faoliyat yurituvchi, bir tomondan, va\n\n"
        f"{f['counterparty']} (keyingi o'rinlarda \"Pudratchi\" deb yuritiladi), "
        f"STIR {f['counterparty_inn'] or BLANK}, rahbar {f['counterparty_director'] or BLANK} shaxsida, "
        f"tegishli litsenziya asosida faoliyat yurituvchi, ikkinchi tomondan,\n\n"
        f"O'zR FK 631-moddasi asosida quyidagi shartnomani tuzdilar:\n\n"
        #
        f"1. SHARTNOMA PREDMETI\n\n"
        f"1.1. Pudratchi Buyurtmachining topshirig'iga binoan quyidagi ishlarni "
        f"o'z xavfi bilan bajarishni o'z zimmasiga oladi (FK 631):\n\n"
        f"{work_description or BLANK}\n\n"
        f"1.2. Shartnomaning umumiy qiymati: {amount_s} so'm (QQS alohida).\n\n"
        #
        f"2. ISH MUDDATLARI\n\n"
        f"2.1. Ish boshlash sanasi: {work_start}.\n"
        f"2.2. Ish tugash sanasi: {work_end}.\n"
        f"2.3. Oraliq muddatlar shartnoma ilovasida belgilanadi.\n\n"
        #
        f"3. MATERIALLAR (FK 635-636)\n\n"
        f"3.1. {materials_text}\n"
        f"3.2. Material sifati tegishli standartlarga mos bo'lishi kerak.\n\n"
        #
        f"4. NARX VA TO'LOV\n\n"
        f"4.1. Ish narxi: {amount_s} so'm.\n"
        f"4.2. Narx qat'iy bo'lib, o'zgartirilishi faqat yozma kelishuv bilan.\n"
    )
    if prepay_pct:
        content += (
            f"4.3. Oldindan to'lov: {prepay_pct}% — shartnoma imzolangan kundan "
            f"3 bank kuni ichida.\n"
            f"4.4. Qolgan qism: qabul-topshirish dalolatnomasi imzolangandan keyin "
            f"5 bank kuni ichida.\n\n"
        )
    else:
        content += (
            f"4.3. To'lov qabul-topshirish dalolatnomasi imzolangandan keyin "
            f"5 bank kuni ichida.\n\n"
        )

    content += (
        f"5. ISH SIFATI VA QABUL QILISH (FK 647-650)\n\n"
        f"5.1. Ish natijasi O'zR amaldagi standartlari va QuN (qurilish normalari)ga mos bo'lishi kerak.\n"
        f"5.2. Ish tugaganda Pudratchi Buyurtmachini xabardor qiladi.\n"
        f"5.3. Buyurtmachi 5 ish kuni ichida ishni qabul qiladi yoki kamchiliklar "
        f"ro'yxatini taqdim etadi.\n"
        f"5.4. Kamchiliklar Pudratchi hisobidan va muddatida bartaraf etiladi.\n\n"
        #
        f"6. KAFOLAT (FK 652)\n\n"
        f"6.1. Kafolat muddati: ishni topshirgan kundan boshlab {warranty_months} oy.\n"
        f"6.2. Kafolat davomida aniqlangan kamchiliklar Pudratchi hisobidan "
        f"bepul bartaraf etiladi.\n\n"
        #
        f"7. JAVOBGARLIK\n\n"
        f"7.1. Ish muddati buzilsa — har kun uchun shartnoma summasining 0,5% "
        f"miqdorida penya (lekin 50% dan oshmasligi kerak).\n"
        f"7.2. To'lov kechiktirilsa — qarz summasining 0,04% kuniga.\n"
        f"7.3. Pudratchi sifatsiz ish uchun to'liq moddiy javobgar (FK 653).\n\n"
        #
        f"8. FORS-MAJOR\n\n"
        f"8.1. Tabiiy ofatlar, urush, davlat qarorlari — fors-major.\n"
        f"8.2. Fors-major yuz berganda 3 kun ichida yozma xabar berish.\n\n"
        #
        f"9. NIZOLARNI HAL QILISH\n\n"
        f"9.1. Muzokaralar yo'li bilan.\n"
        f"9.2. Sudgacha talabnoma (10 kun javob) majburiy.\n"
        f"9.3. Nizo iqtisodiy sudda ko'riladi.\n\n"
        #
        f"10. SHARTNOMA MUDDATI\n\n"
        f"10.1. Shartnoma imzolangan kundan {valid_until or work_end} gacha.\n"
        f"10.2. Shartnoma 2 nusxada tuzilgan.\n\n"
        f"TOMONLAR REKVIZITLARI VA IMZOLARI\n\n"
        f"Buyurtmachi:\n{_rekvizit(f['company'], f['inn'], f['address'], f['bank'], f['account'], f['mfo'], f['director'])}\n\n"
        f"Pudratchi:\n{_rekvizit(f['counterparty'], f['counterparty_inn'], f['counterparty_address'], f['counterparty_bank'], f['counterparty_account'], f['counterparty_mfo'], f['counterparty_director'])}"
    )
    return content


# ═══════════════════════════════════════════════════════════
# NDA / TIJORAT SIRI SHARTNOMASI (O'RQ-370, 2014)
# ═══════════════════════════════════════════════════════════

def generate_nda(params: dict) -> str:
    """NDA — tijorat siri to'g'risida shartnoma.

    Huquqiy asos: O'zR Qonuni "Tijorat siri to'g'risida" (2014, O'RQ-370).
    """
    f = _common_fields(params)
    nda_type = params.get("nda_type", "ikki_tomonlama")  # bir_tomonlama, ikki_tomonlama
    valid_months = params.get("valid_months", 24)
    penalty_amount = params.get("penalty_amount", 0)
    confidential_info = params.get("confidential_info", "")

    if nda_type == "bir_tomonlama":
        discloser = f["company"]
        receiver = f["counterparty"]
        discloser_dir = f["director"]
        receiver_dir = f["counterparty_director"]
    else:
        discloser = ""
        receiver = ""
        discloser_dir = ""
        receiver_dir = ""

    penalty_s = _amount_str(penalty_amount) if penalty_amount else "shartnoma summasining 100%"

    content = (
        f"MAXFIYLIK SHARTNOMASI (NDA) No {f['number']}\n"
        f"(Tijorat siri to'g'risida)\n"
        f"(O'zR \"Tijorat siri to'g'risida\" Qonuni, O'RQ-370, 2014 asosida)\n\n"
        f"{f['city']} shahri{' ' * 40}{f['date']}\n\n"
    )

    if nda_type == "bir_tomonlama":
        content += (
            f"{discloser} (keyingi o'rinlarda \"Oshkor qiluvchi tomon\" deb yuritiladi), "
            f"STIR {f['inn'] or BLANK}, rahbar {discloser_dir or BLANK} shaxsida, "
            f"bir tomondan, va\n\n"
            f"{receiver} (keyingi o'rinlarda \"Qabul qiluvchi tomon\" deb yuritiladi), "
            f"STIR {f['counterparty_inn'] or BLANK}, rahbar {receiver_dir or BLANK} shaxsida, "
            f"ikkinchi tomondan,\n\n"
        )
    else:
        content += (
            f"{f['company']} (STIR {f['inn'] or BLANK}), rahbar {f['director'] or BLANK} shaxsida, "
            f"bir tomondan, va\n\n"
            f"{f['counterparty']} (STIR {f['counterparty_inn'] or BLANK}), "
            f"rahbar {f['counterparty_director'] or BLANK} shaxsida, "
            f"ikkinchi tomondan,\n\n"
            f"(birgalikda \"Tomonlar\" deb yuritiladi)\n\n"
        )

    content += (
        f"quyidagilar to'g'risida mazkur maxfiylik shartnomani tuzdilar:\n\n"
        #
        f"1. ASOSIY TUSHUNCHALAR\n\n"
        f"1.1. \"Maxfiy axborot\" — Tomonlarning tijorat faoliyati bilan bog'liq, "
        f"uchinchi shaxslarga ma'lum bo'lmagan va qonunga muvofiq himoyalanadigan "
        f"barcha ma'lumotlar.\n"
        f"1.2. Maxfiy axborot tarkibiga quyidagilar kiradi:\n"
    )
    if confidential_info:
        content += f"{confidential_info}\n\n"
    else:
        content += (
            f"  a) moliyaviy ma'lumotlar (daromad, foyda, budjet, narxlar);\n"
            f"  b) texnologik ma'lumotlar (dasturiy ta'minot, ishlab chiqarish sirlari);\n"
            f"  c) tijorat ma'lumotlari (mijozlar, yetkazib beruvchilar, shartnomalar);\n"
            f"  d) strategik rejalar va marketing ma'lumotlari;\n"
            f"  e) xodimlar to'g'risidagi ma'lumotlar.\n\n"
        )

    content += (
        f"2. MAJBURIYATLAR\n\n"
        f"2.1. Tomonlar maxfiy axborotni:\n"
        f"  a) uchinchi shaxslarga oshkor qilmaslik;\n"
        f"  b) faqat shartnomada belgilangan maqsadlarda foydalanish;\n"
        f"  c) xavfsiz saqlash uchun barcha zarur choralarni ko'rish;\n"
        f"  d) faqat vakolatli xodimlarga foydalanish huquqini berish.\n\n"
        f"2.2. Maxfiy axborotga kirish huquqi bo'lgan barcha xodimlar "
        f"shaxsiy maxfiylik majburiyatnomasini imzolashlari shart.\n\n"
        #
        f"3. MAXFIY AXBOROT HISOBLANMAYDIGAN MA'LUMOTLAR\n\n"
        f"3.1. Quyidagilar maxfiy hisoblanmaydi:\n"
        f"  a) ommaga ma'lum bo'lgan ma'lumotlar;\n"
        f"  b) qabul qiluvchi tomon oldindan egalik qilgan ma'lumotlar;\n"
        f"  c) uchinchi shaxslardan qonuniy yo'l bilan olingan ma'lumotlar;\n"
        f"  d) qonun talabiga binoan davlat organlariga taqdim etiladigan ma'lumotlar.\n\n"
        #
        f"4. SHARTNOMA MUDDATI\n\n"
        f"4.1. Shartnoma imzolangan kundan boshlab kuchga kiradi.\n"
        f"4.2. Maxfiylik majburiyati shartnoma muddati tugaganidan keyin ham "
        f"{valid_months} oy davomida amal qiladi.\n\n"
        #
        f"5. JAVOBGARLIK\n\n"
        f"5.1. Maxfiy axborot oshkor qilingan taqdirda aybdor tomon:\n"
        f"  a) jarima: {penalty_s} so'm;\n"
        f"  b) etkazilgan haqiqiy zararni to'liq qoplash;\n"
        f"  c) O'RQ-370 ga asosan jinoiy javobgarlik yuzaga kelishi mumkin.\n"
        f"5.2. Oshkor qilish faktini isbotlash burchsi oshkor qilishda ayblaydigan "
        f"tomonga yuklatiladi.\n\n"
        #
        f"6. NIZOLARNI HAL QILISH\n\n"
        f"6.1. Nizolar muzokaralar yo'li bilan hal qilinadi.\n"
        f"6.2. Sudgacha talabnoma (10 kun) majburiy.\n"
        f"6.3. Nizo iqtisodiy sudda ko'riladi.\n\n"
        #
        f"7. YAKUNIY QOIDALAR\n\n"
        f"7.1. Shartnoma 2 nusxada tuzilgan.\n"
        f"7.2. Maxfiy axborotni qaytarish: shartnoma tugaganda barcha nusxalar "
        f"(qog'oz va elektron) qaytariladi yoki yo'q qilinadi.\n\n"
        f"TOMONLAR IMZOLARI\n\n"
        f"{f['company']}:\n"
        f"Rahbar: _______________ / {f['director'] or BLANK} /\n"
        f"M.O.\n\n"
        f"{f['counterparty']}:\n"
        f"Rahbar: _______________ / {f['counterparty_director'] or BLANK} /\n"
        f"M.O."
    )
    return content


# ═══════════════════════════════════════════════════════════
# TUSHUNTIRISH XATI (Explanation Letter)
# ═══════════════════════════════════════════════════════════

def generate_tushuntirish_xati(params: dict) -> str:
    """Tushuntirish xati — intizomiy jazo berish uchun talab qilinadi (MK 181)."""
    f = _common_fields(params)
    employee_name = params.get("employee_name", "")
    position = params.get("position", BLANK)
    department = params.get("department", "")
    incident_date = params.get("incident_date", BLANK)
    incident_description = params.get("incident_description", f["description"])

    content = (
        f"{f['company']}\n"
        f"Rahbar {f['director'] or BLANK} ga\n\n"
        f"TUSHUNTIRISH XATI\n\n"
        f"Sana: {f['date']}\n\n"
        f"Men, {employee_name or BLANK}, {position} lavozimida ishlayman"
    )
    if department:
        content += f" ({department} bo'limida)"
    content += (
        f".\n\n"
        f"Sana {incident_date} da sodir bo'lgan voqea haqida quyidagilarni "
        f"tushuntiraman:\n\n"
        f"{incident_description or BLANK}\n\n"
        f"Eslatma: MK 181-moddaga asosan, ish beruvchi intizomiy jazo berishdan "
        f"oldin xodimdan tushuntirish xatini talab qilishi shart. "
        f"Xodim 2 ish kuni ichida tushuntirish xatini taqdim etishi kerak. "
        f"Taqdim etmasa, bu intizomiy jazo berishga to'sqinlik qilmaydi.\n\n"
        f"Imzo: _______________ / {employee_name or BLANK} /\n"
        f"Sana: {f['date']}"
    )
    return content


# ═══════════════════════════════════════════════════════════
# ARIZA SHAKLLARI (Application Forms)
# ═══════════════════════════════════════════════════════════

def generate_ariza(params: dict) -> str:
    """Ariza — ishga, ta'tilga, bo'shatishga ariza shakllari."""
    f = _common_fields(params)
    ariza_type = params.get("ariza_type", "ishga")  # ishga, tatilga, boshatish
    employee_name = params.get("employee_name", "")
    position = params.get("position", BLANK)
    department = params.get("department", "")
    desired_date = params.get("desired_date", BLANK)
    reason = params.get("reason", "")

    if ariza_type == "ishga":
        content = (
            f"{f['company']} rahbari\n"
            f"{f['director'] or BLANK} ga\n\n"
            f"fuqaro {employee_name or BLANK} dan\n"
            f"Manzil: {f['counterparty_address'] or BLANK}\n"
            f"Telefon: {BLANK}\n\n"
            f"ARIZA\n\n"
            f"Sizning tashkilotingizga {desired_date} dan boshlab "
            f"{position} lavozimiga"
        )
        if department:
            content += f" ({department} bo'limiga)"
        content += (
            f" ishga qabul qilishingizni so'rayman.\n\n"
            f"Ilova:\n"
            f"  1. Pasport nusxasi\n"
            f"  2. Mehnat daftarchasi\n"
            f"  3. Diplom nusxasi\n"
            f"  4. 3x4 rasm (2 dona)\n"
            f"  5. Tibbiy ma'lumotnoma (086-shakl)\n\n"
        )
    elif ariza_type == "tatilga":
        leave_type = params.get("leave_type", "yillik")
        leave_days = params.get("leave_days", 15)
        leave_from = params.get("leave_from", desired_date)

        leave_type_text = {
            "yillik": "yillik mehnat ta'tili",
            "haqi_saqlanmaydigan": "mehnat haqi saqlanmaydigan ta'til",
            "oquv": "o'quv ta'tili",
        }.get(leave_type, leave_type)

        content = (
            f"{f['company']} rahbari\n"
            f"{f['director'] or BLANK} ga\n\n"
            f"{position} {employee_name or BLANK} dan\n"
        )
        if department:
            content += f"({department} bo'limi)\n"
        content += (
            f"\nARIZA\n\n"
            f"Menga {leave_from} dan boshlab {leave_days} kalendar kun "
            f"muddatga {leave_type_text} berishingizni so'rayman.\n"
        )
        if reason:
            content += f"\nSabab: {reason}\n"
        content += "\n"

    elif ariza_type == "boshatish":
        content = (
            f"{f['company']} rahbari\n"
            f"{f['director'] or BLANK} ga\n\n"
            f"{position} {employee_name or BLANK} dan\n"
        )
        if department:
            content += f"({department} bo'limi)\n"
        content += (
            f"\nARIZA\n\n"
            f"Meni {desired_date} dan boshlab o'z xohishimga ko'ra "
            f"(MK 99-modda) ishdan bo'shatishingizni so'rayman.\n"
        )
        if reason:
            content += f"\nSabab: {reason}\n"
        content += (
            f"\nEslatma: MK 99-moddaga asosan xodim kamida 2 hafta oldin "
            f"yozma ariza berishi shart.\n\n"
        )
    else:
        content = (
            f"{f['company']} rahbari\n"
            f"{f['director'] or BLANK} ga\n\n"
            f"{employee_name or BLANK} dan\n\n"
            f"ARIZA\n\n"
            f"{f['description'] or BLANK}\n\n"
        )

    content += (
        f"Imzo: _______________ / {employee_name or BLANK} /\n"
        f"Sana: {f['date']}"
    )
    return content


# ═══════════════════════════════════════════════════════════
# Hujjat turlari registry
# ═══════════════════════════════════════════════════════════

TIER1_GENERATORS: dict[str, callable] = {
    "oldi_sotdi": generate_oldi_sotdi,
    "yetkazib_berish": generate_yetkazib_berish,
    "ijara": generate_ijara,
    "mehnat": generate_mehnat,
    "solishtirma": generate_solishtirma,
    "tijorat_taklifi": generate_tijorat_taklifi,
    # TIER 2
    "xizmat": generate_xizmat,
    "qabul_topshirish": generate_qabul_topshirish,
    "buyruq_t1": generate_buyruq_t1,
    "buyruq_t6": generate_buyruq_t6,
    "buyruq_t8": generate_buyruq_t8,
    "pudrat": generate_pudrat,
    "nda": generate_nda,
    "tushuntirish_xati": generate_tushuntirish_xati,
    "ariza": generate_ariza,
}

# Additional parameters specific to TIER 1 documents
TIER1_EXTRA_PARAMS: dict = {
    "delivery_date": {"type": "string", "description": "Yetkazib berish sanasi (oldi-sotdi/yetkazib berish)"},
    "delivery_place": {"type": "string", "description": "Yetkazib berish joyi"},
    "delivery_schedule": {"type": "string", "description": "Yetkazib berish jadvali (yetkazib berish: har oyda, har hafta)"},
    "warranty_months": {"type": "number", "description": "Kafolat muddati (oy, oldi-sotdi)"},
    "payment_type": {"type": "string", "description": "To'lov turi: bank, cash, mixed"},
    "prepay_pct": {"type": "number", "description": "Oldindan to'lov foizi (%)"},
    "acceptance_days": {"type": "number", "description": "Qabul qilish muddati (ish kuni)"},
    "payment_days": {"type": "number", "description": "To'lov muddati (bank kuni)"},
    "object_type": {"type": "string", "description": "Ijara ob'ekti turi (bino, xona, er, transport)"},
    "object_description": {"type": "string", "description": "Ijara ob'ekti tavsifi"},
    "object_area": {"type": "string", "description": "Maydon (kv.m)"},
    "object_address": {"type": "string", "description": "Ob'ekt manzili"},
    "cadastral_number": {"type": "string", "description": "Kadastr raqami (ijara)"},
    "rent_amount": {"type": "number", "description": "Ijara haqi (oylik)"},
    "rent_period": {"type": "string", "description": "Ijara haqi davri: oylik, choraklik, yillik"},
    "utilities_included": {"type": "boolean", "description": "Kommunal xizmatlar ijara haqiga kiradimi"},
    "purpose": {"type": "string", "description": "Foydalanish maqsadi (ijara)"},
    "valid_from": {"type": "string", "description": "Boshlanish sanasi"},
    "employee_name": {"type": "string", "description": "Xodim F.I.Sh. (mehnat)"},
    "passport": {"type": "string", "description": "Pasport seriya va raqami"},
    "position": {"type": "string", "description": "Lavozim"},
    "department": {"type": "string", "description": "Bo'lim"},
    "salary": {"type": "number", "description": "Mehnat haqi (so'mda)"},
    "salary_type": {"type": "string", "description": "Haq turi: oylik, kunlik, ishbay"},
    "work_schedule": {"type": "string", "description": "Ish jadvali (masalan: 09:00-18:00)"},
    "probation_months": {"type": "number", "description": "Sinov muddati (oy, max 3)"},
    "contract_type": {"type": "string", "description": "Shartnoma turi: muddatsiz, muddatli"},
    "vacation_days": {"type": "number", "description": "Yillik ta'til (ish kuni, default 15)"},
    "start_date": {"type": "string", "description": "Ishga kirish sanasi"},
    "period_from": {"type": "string", "description": "Davr boshi (solishtirma)"},
    "period_to": {"type": "string", "description": "Davr oxiri (solishtirma)"},
    "opening_balance": {"type": "number", "description": "Boshlang'ich qoldiq (solishtirma)"},
    "opening_balance_side": {"type": "string", "description": "Qoldiq tomoni: debit yoki kredit"},
    "operations": {
        "type": "array",
        "description": "Operatsiyalar ro'yxati (solishtirma): [{date, description, debit, credit}]",
        "items": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "description": {"type": "string"},
                "debit": {"type": "number"},
                "credit": {"type": "number"},
            },
        },
    },
    "valid_days": {"type": "number", "description": "Taklif amal qilish muddati (kun, default 30)"},
    "delivery_terms": {"type": "string", "description": "Yetkazib berish shartlari (tijorat taklifi)"},
    "payment_terms": {"type": "string", "description": "To'lov shartlari (tijorat taklifi)"},
    "special_conditions": {"type": "string", "description": "Qo'shimcha shartlar"},
    "contact_person": {"type": "string", "description": "Bog'lanish uchun shaxs"},
    "contact_phone": {"type": "string", "description": "Telefon raqam"},
    "contact_email": {"type": "string", "description": "Email"},
}
