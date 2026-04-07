"""Shartnoma shablonlari — oldi-sotdi, yetkazib berish, ijara, pudrat."""

from __future__ import annotations

from qanot.tools.doc_templates.helpers import BLANK, _rekvizit, _amount_str, _common_fields


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
