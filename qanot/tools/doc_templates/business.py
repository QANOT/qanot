"""Biznes hujjatlari — solishtirma, tijorat taklifi, xizmat, qabul-topshirish, NDA."""

from __future__ import annotations

from qanot.tools.doc_templates.helpers import BLANK, _rekvizit, _amount_str, _common_fields


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
