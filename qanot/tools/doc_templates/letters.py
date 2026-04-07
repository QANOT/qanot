"""Xat shakllari — tushuntirish xati, ariza."""

from __future__ import annotations

from qanot.tools.doc_templates.helpers import BLANK, _common_fields


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
