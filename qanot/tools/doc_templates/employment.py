"""Mehnat hujjatlari — mehnat shartnomasi, buyruqlar (T-1, T-6, T-8)."""

from __future__ import annotations

from qanot.tools.doc_templates.helpers import BLANK, _amount_str, _common_fields


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
