"""O'zbekiston biznes toollar — valyuta kursi, IKPU, to'lov havolasi, kalkulyatorlar.

Split into domain modules:
  - local_currency.py: CBU exchange rates
  - local_ikpu.py: IKPU product classification codes
  - local_payment.py: Click/Payme payment links
  - local_calculator.py: VAT, turnover tax, markup, installment calculators
  - local_weather.py: Weather forecasts
  - generate_document: stays here (depends on document_templates.py)
"""

from __future__ import annotations

import json
import logging

from qanot.registry import ToolRegistry
from qanot.tools.local_currency import register_currency_tools
from qanot.tools.local_ikpu import register_ikpu_tools
from qanot.tools.local_payment import register_payment_tools
from qanot.tools.local_calculator import register_calculator_tools
from qanot.tools.local_weather import register_weather_tools

logger = logging.getLogger(__name__)


def register_local_tools(registry: ToolRegistry) -> None:
    """Register Uzbekistan-specific business tools."""

    register_currency_tools(registry)
    register_ikpu_tools(registry)
    register_payment_tools(registry)
    register_calculator_tools(registry)

    # ═══════════════════════════════════════
    # HUJJAT GENERATORI (Document templates)
    # ═══════════════════════════════════════

    async def generate_document(params: dict) -> str:
        """Biznes hujjat yaratish — 11 xil hujjat turi, O'zR qonunchiligiga mos."""
        from qanot.tools.doc_templates import TIER1_GENERATORS

        doc_type = params.get("type", "shartnoma").lower().strip()

        # TIER 1 hujjat turlarini alohida moduldan chaqirish
        if doc_type in TIER1_GENERATORS:
            try:
                content = TIER1_GENERATORS[doc_type](params)
                return json.dumps({
                    "type": doc_type,
                    "content": content,
                    "filename": f"{doc_type}_{params.get('number', '1')}_{params.get('date', 'sana').replace('.', '-')}.txt",
                    "hint": "Bu matnni create_docx tool bilan DOCX faylga saqlash mumkin, yoki to'g'ridan-to'g'ri foydalanuvchiga ko'rsatish mumkin.",
                }, ensure_ascii=False)
            except Exception as e:
                logger.error("TIER1 document generation error for %s: %s", doc_type, e)
                return json.dumps({"error": f"Hujjat yaratishda xatolik: {e}"})

        # Common fields
        company = params.get("company", "KOMPANIYA NOMI")
        director = params.get("director", "F.I.O.")
        inn = params.get("inn", "")
        date = params.get("date", "___.___.2024")

        def _rekvizit(prefix: str = "") -> str:
            p = f"{prefix}_" if prefix else ""
            c = params.get(f"{p}company", company)
            d = params.get(f"{p}director", director)
            i = params.get(f"{p}inn", inn)
            bank = params.get(f"{p}bank", "")
            account = params.get(f"{p}account", "")
            mfo = params.get(f"{p}mfo", "")
            address = params.get(f"{p}address", "")
            phone = params.get(f"{p}phone", "")
            lines = [f"Tashkilot: {c}", f"Rahbar: {d}"]
            if i:
                lines.append(f"INN/STIR: {i}")
            if bank:
                lines.append(f"Bank: {bank}")
            if account:
                lines.append(f"H/r: {account}")
            if mfo:
                lines.append(f"MFO: {mfo}")
            if address:
                lines.append(f"Manzil: {address}")
            if phone:
                lines.append(f"Tel: {phone}")
            return "\n".join(lines)

        def _common_fields() -> str:
            return (
                f"Sana: {date}\n"
                f"Tashkilot: {company}\n"
                f"Rahbar: {director}\n"
            )

        if doc_type == "shartnoma":
            subject = params.get("subject", "xizmat ko'rsatish")
            amount = params.get("amount", "___________")
            duration = params.get("duration", "1 (bir) yil")
            number = params.get("number", "___")
            city = params.get("city", "Toshkent sh.")

            content = f"""SHARTNOMA № {number}

{city}                                                    {date}

{company} (keyingi o'rinlarda "Buyurtmachi") nomidan {director} bir tomondan,
va ________________ (keyingi o'rinlarda "Bajaruvchi") nomidan ________________ ikkinchi tomondan,
quyidagi shartnomani tuzdilar:

1. SHARTNOMA PREDMETI
1.1. Bajaruvchi Buyurtmachiga {subject} bo'yicha xizmat ko'rsatishni o'z zimmasiga oladi.
1.2. Xizmatlar sifati O'zbekiston Respublikasi qonunchiligiga muvofiq bo'lishi kerak.

2. SHARTNOMA SUMMASI VA TO'LOV TARTIBI
2.1. Shartnoma summasi: {amount} so'm (QQS bilan).
2.2. To'lov bank o'tkazmasi orqali amalga oshiriladi.
2.3. To'lov Bajaruvchi tomonidan dalolatnoma imzolangandan so'ng 5 (besh) bank kunida amalga oshiriladi.

3. TOMONLARNING MAJBURIYATLARI
3.1. Bajaruvchi:
  - Xizmatlarni sifatli va o'z vaqtida bajarish
  - Buyurtmachini ish borishi to'g'risida xabardor qilish
3.2. Buyurtmachi:
  - To'lovni o'z vaqtida amalga oshirish
  - Xizmat ko'rsatish uchun zarur ma'lumotlarni taqdim etish

4. SHARTNOMA MUDDATI
4.1. Ushbu shartnoma {duration} muddatga tuzilgan.
4.2. Shartnoma imzolangan kundan boshlab kuchga kiradi.

5. NIZOLARNI HAL ETISH
5.1. Tomonlar o'rtasidagi nizolar muzokaralar yo'li bilan hal qilinadi.
5.2. Kelishuvga erishilmagan taqdirda nizo iqtisod sudi orqali hal etiladi.

6. TOMONLARNING REKVIZITLARI

BUYURTMACHI:                              BAJARUVCHI:
{_rekvizit()}                    ________________________

_____________ / {director} /              _____________ / __________ /
      M.O.                                      M.O.
"""
        elif doc_type == "faktura":
            number = params.get("number", "___")
            items = params.get("items", [])
            buyer = params.get("buyer_company", "")

            items_text = ""
            total = 0
            for i, item in enumerate(items if isinstance(items, list) else [], 1):
                name = item.get("name", "") if isinstance(item, dict) else str(item)
                qty = item.get("quantity", 1) if isinstance(item, dict) else 1
                price = item.get("price", 0) if isinstance(item, dict) else 0
                summa = qty * price
                total += summa
                items_text += f"| {i} | {name} | {qty} | {price:,.0f} | {summa:,.0f} |\n"

            vat = round(total * 12 / 112)
            content = f"""HISOB-FAKTURA № {number}

Sana: {date}

Yetkazib beruvchi: {company}
INN: {inn}
Rahbar: {director}

Xaridor: {buyer}

TOVARLAR RO'YXATI:
| № | Nomi | Soni | Narxi | Summasi |
|---|------|------|-------|---------|
{items_text}
Jami: {total:,.0f} so'm
Shu jumladan QQS (12%): {vat:,.0f} so'm

Rahbar: _____________ / {director} /
Bosh hisobchi: _____________ / __________ /
M.O.
"""
        elif doc_type == "dalolatnoma":
            number = params.get("number", "___")
            work_description = params.get("work_description", "bajarilgan ishlar")
            amount = params.get("amount", "___________")

            content = f"""BAJARILGAN ISHLAR DALOLATNOMASI № {number}

{params.get('city', 'Toshkent sh.')}                                          {date}

Biz, quyida imzo chekuvchilar:
Buyurtmachi — {company} nomidan {director},
Bajaruvchi — ________________ nomidan ________________,

Ushbu dalolatnomani tuzdik:

1. Bajaruvchi quyidagi ishlarni to'liq va sifatli bajargan:
   {work_description}

2. Ishlar shartnoma shartlariga muvofiq bajarilgan.

3. Bajarilgan ishlar summasi: {amount} so'm (QQS bilan).

4. Buyurtmachi bajarilgan ishlarga e'tiroz bildirmaydi.

BUYURTMACHI:                              BAJARUVCHI:
_____________ / {director} /              _____________ / __________ /
      M.O.                                      M.O.
"""
        elif doc_type == "ishonchnoma":
            number = params.get("number", "___")
            trusted_person = params.get("trusted_person", "F.I.O.")
            passport = params.get("passport", "AA 0000000")
            purpose = params.get("purpose", "mol-mulk olish va topshirish")
            valid_until = params.get("valid_until", "___.___.2024")

            content = f"""ISHONCHNOMA № {number}

{params.get('city', 'Toshkent sh.')}                                          {date}

{company} (INN: {inn}) nomidan {director} ushbu ishonchnoma bilan

{trusted_person}
Passport: {passport}

ga quyidagi vakolatlarni beradi:
{purpose}

Ishonchnoma {valid_until} gacha amal qiladi.
Boshqa shaxslarga vakolat o'tkazish huquqisiz.

Rahbar: _____________ / {director} /
M.O.
"""
        elif doc_type == "talabnoma":
            number = params.get("number", "___")
            recipient_company = params.get("recipient_company", "________________")
            claim_description = params.get("claim_description", "")
            amount = params.get("amount", "___________")
            deadline = params.get("deadline", "10 (o'n) ish kuni")

            content = f"""TALABNOMA (PRETENZIYA) № {number}

Kimga: {recipient_company}
Kimdan: {company}
Sana: {date}

Hurmatli rahbar,

{company} va Sizning kompaniyangiz o'rtasida tuzilgan shartnomaga asosan,
quyidagi talabni bildiramiz:

{claim_description}

Qarz summasi: {amount} so'm

O'zbekiston Respublikasi Fuqarolik kodeksining 330-moddasiga asosan,
ushbu summani {deadline} ichida to'lashingizni talab qilamiz.

Mazkur talabnoma qanoatlantirilmagan taqdirda, {company} sudga
murojaat qilish huquqini o'zida saqlab qoladi.

Ilova qilingan hujjatlar:
1. Shartnoma nusxasi
2. Hisob-faktura nusxasi
3. Yetkazib berish dalolatnomasi

Hurmat bilan,
{director}
{company}

_____________ / {director} /
M.O.
"""
        else:
            all_types = ["shartnoma", "faktura", "dalolatnoma", "ishonchnoma", "talabnoma"]
            all_types.extend(sorted(TIER1_GENERATORS.keys()))
            types_str = ", ".join(all_types)
            return json.dumps({
                "error": f"Noma'lum hujjat turi: {doc_type}. Mavjud turlar: {types_str}",
            }, ensure_ascii=False)

        # Return content for agent to use with create_docx or send directly
        return json.dumps({
            "document_type": doc_type,
            "content": content.strip(),
            "filename": f"{doc_type}_{date.replace('.', '_')}.txt",
            "note": "Ushbu hujjat matnini create_docx tool bilan DOCX formatga o'tkazishingiz mumkin.",
        }, ensure_ascii=False)

    registry.register(
        name="generate_document",
        description="Generate Uzbek business documents: shartnoma, faktura, dalolatnoma, ishonchnoma, talabnoma, oldi-sotdi, yetkazib berish, ijara, mehnat shartnomasi, solishtirma dalolatnoma, tijorat taklifi.",
        parameters={
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {
                    "type": "string",
                    "description": "Hujjat turi: shartnoma | faktura | dalolatnoma | ishonchnoma | talabnoma | oldi_sotdi | yetkazib_berish | ijara | mehnat | solishtirma | tijorat_taklifi",
                },
                "company": {"type": "string", "description": "Tashkilot nomi"},
                "director": {"type": "string", "description": "Rahbar F.I.O."},
                "inn": {"type": "string", "description": "INN/STIR"},
                "date": {"type": "string", "description": "Sana (DD.MM.YYYY)"},
                "number": {"type": "string", "description": "Hujjat raqami"},
                "amount": {"type": "string", "description": "Summa"},
                "subject": {"type": "string", "description": "Shartnoma predmeti"},
                "duration": {"type": "string", "description": "Muddat"},
                "items": {
                    "type": "array",
                    "description": "Tovarlar ro'yxati (faktura uchun)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "quantity": {"type": "number"},
                            "price": {"type": "number"},
                        },
                    },
                },
                "city": {"type": "string", "description": "Shahar"},
                "buyer_company": {"type": "string", "description": "Xaridor kompaniyasi"},
                "work_description": {"type": "string", "description": "Bajarilgan ishlar tavsifi"},
                "trusted_person": {"type": "string", "description": "Ishonchli shaxs F.I.O."},
                "passport": {"type": "string", "description": "Passport ma'lumotlari"},
                "purpose": {"type": "string", "description": "Maqsad / vakolat"},
                "valid_until": {"type": "string", "description": "Amal qilish muddati"},
                "recipient_company": {"type": "string", "description": "Qabul qiluvchi tashkilot"},
                "claim_description": {"type": "string", "description": "Talab/da'vo tavsifi"},
                "deadline": {"type": "string", "description": "Bajarish muddati"},
            },
        },
        handler=generate_document,
        category="document",
    )

    register_weather_tools(registry)

    logger.info("Local tools registered: currency_rate, ikpu_search, payment_link, tax_calculator, generate_document, weather")
