"""Biznes hujjat shablonlari — O'zR qonunchiligiga mos."""

from __future__ import annotations

from qanot.tools.doc_templates.helpers import BLANK, _rekvizit, _amount_str, _common_fields
from qanot.tools.doc_templates.contracts import generate_oldi_sotdi, generate_yetkazib_berish, generate_ijara, generate_pudrat
from qanot.tools.doc_templates.employment import generate_mehnat, generate_buyruq_t1, generate_buyruq_t6, generate_buyruq_t8
from qanot.tools.doc_templates.business import generate_solishtirma, generate_tijorat_taklifi, generate_xizmat, generate_qabul_topshirish, generate_nda
from qanot.tools.doc_templates.letters import generate_tushuntirish_xati, generate_ariza

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
