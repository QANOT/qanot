"""Supported document types the scanner plugin knows how to extract + route.

Each DOCTYPE describes:
  - key           : programmatic name (agent + SOUL use this)
  - uzbek_names   : colloquial names the user might say ("chek", "vizitka")
  - description   : one-line description (Uzbek, shown to the user on demand)
  - fields        : list of fields to extract with type hints
  - default_output: which save path the agent should default to if user
                    doesn't specify (one of: 'sheet', 'xlsx', 'pdf', 'docx',
                    'crm_contact', 'crm_deal')
  - default_sheet : for 'sheet' output, the tab name convention
  - sensitive     : True if the doc may contain PII/financial data that
                    should be handled locally (no CRM, no cloud sync)

The SOUL prompt references this knowledge base. Keeping it in Python (not
in SOUL markdown) lets us expose `scanner_doctypes()` as a lookup tool AND
run unit tests over the structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

OutputFormat = Literal[
    "sheet",
    "xlsx",
    "pdf",
    "docx",
    "crm_contact",
    "crm_deal",
]


@dataclass(frozen=True)
class DocType:
    key: str
    uzbek_names: tuple[str, ...]
    description: str
    fields: tuple[str, ...]
    default_output: OutputFormat
    default_sheet: str | None = None
    sensitive: bool = False
    notes: str = ""


DOCTYPES: tuple[DocType, ...] = (
    DocType(
        key="receipt",
        uzbek_names=("chek", "kvitansiya", "kvitansiya-chek"),
        description="Do'kon/savdo cheki — xarajat sifatida qayd etiladi.",
        fields=(
            "date",        # ISO YYYY-MM-DD
            "vendor",      # shop/vendor name
            "total",       # amount as number, no currency symbol
            "currency",    # UZS default, USD if detected
            "items",       # optional list of item descriptions
            "category",    # from expense category enum (see categorize.py)
            "fiscal_id",   # if QR code readable, the 12-digit fiscal mark
        ),
        default_output="sheet",
        default_sheet="Xarajatlar 2026",
        notes=(
            "Duplicate check before save: same (vendor, amount) within 24h → "
            "confirm with user. Category via expense_categorize tool."
        ),
    ),
    DocType(
        key="invoice",
        uzbek_names=("faktura", "invoys", "schet", "e-faktura"),
        description=(
            "B2B faktura (kompaniya→kompaniya). Qayd etiladi + PDF nusxa saqlanadi."
        ),
        fields=(
            "date",
            "invoice_number",
            "seller_name",
            "seller_tin",       # Tax ID Number (STIR)
            "buyer_name",
            "buyer_tin",
            "subtotal",
            "tax",              # NDS (VAT)
            "total",
            "currency",
            "items",            # each: {name, qty, price, total}
            "due_date",         # payment due
        ),
        default_output="sheet",
        default_sheet="Fakturalar 2026",
        notes=(
            "Save TO sheet AND offer PDF copy via create_pdf — "
            "accountants need both structured row + scan copy."
        ),
    ),
    DocType(
        key="business_card",
        uzbek_names=("vizitka", "tashrif qog'ozi", "kontakt kartochkasi"),
        description="Biznes vizitka — kontakt sifatida saqlanadi.",
        fields=(
            "full_name",
            "company",
            "role",              # job title
            "phone",             # normalize to +998XXXXXXXXX
            "email",
            "website",
            "address",
            "social",            # telegram/instagram if present
        ),
        default_output="crm_contact",
        default_sheet="Kontaktlar",
        notes=(
            "If amocrm/bitrix24 plugin is connected, call "
            "amocrm_create_contact or bitrix24_create_contact. "
            "Otherwise append to Kontaktlar sheet."
        ),
    ),
    DocType(
        key="contract",
        uzbek_names=("shartnoma", "kontrakt", "bitim"),
        description=(
            "Shartnoma/kontrakt — asosiy shartlar ajratib olinadi, DOCX "
            "xulosa tayyorlanadi. Asl faylni foydalanuvchi o'zida saqlaydi."
        ),
        fields=(
            "date",
            "parties",            # list of [{name, tin, role}]
            "subject",            # what the contract is about
            "amount",
            "currency",
            "start_date",
            "end_date",
            "payment_terms",
            "termination_clause",
            "key_obligations",    # list of key obligations per party
        ),
        default_output="docx",
        notes=(
            "Create a structured DOCX summary, not a verbatim copy. "
            "Highlight amounts, dates, and any unusual clauses."
        ),
    ),
    DocType(
        key="menu",
        uzbek_names=("menyu", "narxnoma", "prays-list"),
        description=(
            "Restoran menyusi yoki narx ro'yxati — XLSX jadval qilib beriladi."
        ),
        fields=(
            "items",              # list of {name, price, category, description}
            "currency",
            "vendor",             # restaurant/store name if present
            "date_valid",         # "valid from/until" if printed
        ),
        default_output="xlsx",
        notes="XLSX with columns: Nomi, Narxi, Kategoriya, Tavsif.",
    ),
    DocType(
        key="handwritten",
        uzbek_names=("yozuv", "qo'lyozma", "qayd"),
        description=(
            "Qo'lda yozilgan qaydlar — ko'chirib olinadi, PDF yoki Word sifatida beriladi."
        ),
        fields=(
            "raw_text",           # best-effort transcription
            "structure",          # detected: bullets, paragraphs, table, etc.
            "language",           # uz, ru, en, mixed
        ),
        default_output="docx",
        notes=(
            "Ask user: PDF yoki DOCX? DOCX is better for editable notes. "
            "Keep original formatting (bullets as bullets, headers as headers)."
        ),
    ),
    DocType(
        key="product_catalog",
        uzbek_names=("katalog", "mahsulot ro'yxati", "tovar kartochkasi"),
        description=(
            "Mahsulot katalogi — inventar jadvaliga qatorlar qo'shiladi."
        ),
        fields=(
            "items",              # list of {name, sku, price, currency, qty, category}
            "vendor",
        ),
        default_output="sheet",
        default_sheet="Tovarlar",
        notes=(
            "Each item becomes one row. If ibox/moysklad plugin is connected, "
            "offer to sync inventory there too."
        ),
    ),
    DocType(
        key="id_document",
        uzbek_names=("pasport", "id karta", "haydovchilik guvohnomasi", "prava"),
        description=(
            "Shaxsiy hujjat (pasport, ID karta). SHAXSIY MA'LUMOT — "
            "faqat lokal DOCX sifatida saqlanadi, CRM/Sheets-ga yuborilmaydi."
        ),
        fields=(
            "document_type",      # passport, id_card, driver_license
            "full_name",
            "document_number",
            "issue_date",
            "expiry_date",
            "issuing_authority",
            "nationality",
        ),
        default_output="docx",
        sensitive=True,
        notes=(
            "SENSITIVE: PII data. Always ask user to CONFIRM before saving "
            "anywhere. Default: DOCX in workspace only. Never auto-send to "
            "CRM/Sheets/cloud without explicit user consent per save."
        ),
    ),
    DocType(
        key="order_form",
        uzbek_names=("zakaz", "buyurtma", "buyurtma blanki"),
        description=(
            "Mijoz buyurtma blanki — CRM-ga deal yoki Sheets-ga qator qo'shiladi."
        ),
        fields=(
            "customer_name",
            "customer_phone",
            "date",
            "items",              # list of {name, qty, price}
            "total",
            "currency",
            "delivery_address",
            "notes",
        ),
        default_output="crm_deal",
        default_sheet="Buyurtmalar",
        notes=(
            "If amocrm/bitrix24 connected: amocrm_create_complex_lead / "
            "bitrix24_create_deal. Otherwise append to Buyurtmalar sheet."
        ),
    ),
)


# Lookup helpers ---------------------------------------------------

_BY_KEY: dict[str, DocType] = {dt.key: dt for dt in DOCTYPES}


def get_doctype(key: str) -> DocType | None:
    return _BY_KEY.get(key)


def find_by_uzbek_name(name: str) -> DocType | None:
    """Match a user-provided Uzbek name (case-insensitive) to a doctype."""
    if not name:
        return None
    n = name.strip().lower()
    for dt in DOCTYPES:
        if n in {u.lower() for u in dt.uzbek_names}:
            return dt
    # Partial match — 'chek' in 'chek yozma' should still hit 'receipt'
    for dt in DOCTYPES:
        for u in dt.uzbek_names:
            if u.lower() in n or n in u.lower():
                return dt
    return None


def as_dict_list() -> list[dict]:
    """Serializable form for the scanner_doctypes tool response."""
    return [
        {
            "key": dt.key,
            "uzbek_names": list(dt.uzbek_names),
            "description": dt.description,
            "fields": list(dt.fields),
            "default_output": dt.default_output,
            "default_sheet": dt.default_sheet,
            "sensitive": dt.sensitive,
            "notes": dt.notes,
        }
        for dt in DOCTYPES
    ]
