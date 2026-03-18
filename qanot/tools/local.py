"""O'zbekiston biznes toollar — valyuta kursi, IKPU, to'lov havolasi, kalkulyatorlar."""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)

# CBU API — Markaziy bank rasmiy kurslari (bepul, autentifikatsiyasiz)
CBU_URL = "https://cbu.uz/ru/arkhiv-kursov-valyut/json/"

# IKPU API — Tovar klassifikatori (bepul)
IKPU_URL = "https://tasnif.soliq.uz/api/cl-api/class/search"


def register_local_tools(registry: ToolRegistry) -> None:
    """Register Uzbekistan-specific business tools."""

    # ═══════════════════════════════════════
    # VALYUTA KURSI (Currency rates)
    # ═══════════════════════════════════════

    async def get_currency_rates(params: dict) -> str:
        """Bugungi valyuta kurslari — CBU rasmiy."""
        try:
            currency = params.get("currency", "").upper()
            async with aiohttp.ClientSession() as session:
                async with session.get(CBU_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json(content_type=None)

            if currency:
                # Filter specific currency
                found = [r for r in data if r.get("Ccy", "").upper() == currency]
                if not found:
                    return json.dumps({"error": f"{currency} topilmadi"})
                r = found[0]
                return json.dumps({
                    "currency": r["Ccy"],
                    "rate": r["Rate"],
                    "diff": r["Diff"],
                    "date": r["Date"],
                    "name": r.get("CcyNm_UZ", r.get("CcyNm_RU", "")),
                }, ensure_ascii=False)
            else:
                # Return main currencies
                main = {"USD", "EUR", "RUB", "GBP", "CNY", "KZT", "TRY"}
                result = []
                for r in data:
                    if r.get("Ccy", "") in main:
                        result.append({
                            "currency": r["Ccy"],
                            "rate": r["Rate"],
                            "diff": r["Diff"],
                        })
                return json.dumps({"date": data[0]["Date"] if data else "", "rates": result}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"Kurs olishda xatolik: {e}"})

    registry.register(
        name="currency_rate",
        description="Bugungi valyuta kurslari (CBU rasmiy). Dollar, yevro, rubl va boshqa valyutalar.",
        parameters={
            "type": "object",
            "properties": {
                "currency": {
                    "type": "string",
                    "description": "Valyuta kodi (USD, EUR, RUB). Bo'sh qolsa asosiy valyutalar ko'rsatiladi",
                },
            },
        },
        handler=get_currency_rates,
    )

    # ═══════════════════════════════════════
    # IKPU KOD QIDIRISH
    # ═══════════════════════════════════════

    async def search_ikpu(params: dict) -> str:
        """IKPU (MXIK) tovar kodini qidirish."""
        query = params.get("query", "").strip()
        if not query:
            return json.dumps({"error": "Qidiruv so'zini kiriting"})
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    IKPU_URL,
                    params={"keyword": query, "page": 0, "size": 10, "lang": "uz"},
                    headers={"Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)

            items = data.get("content", data.get("data", []))
            if not items:
                return json.dumps({"error": f"'{query}' bo'yicha IKPU topilmadi"})

            results = []
            for item in items[:10]:
                results.append({
                    "code": item.get("mxikCode", item.get("code", "")),
                    "name": item.get("mxikFullNameUz", item.get("nameUz", item.get("name", ""))),
                    "units": item.get("unitName", ""),
                })
            return json.dumps({"query": query, "results": results}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"IKPU qidirishda xatolik: {e}"})

    registry.register(
        name="ikpu_search",
        description="IKPU (MXIK) tovar klassifikator kodini qidirish. Tovar nomi bo'yicha 17 raqamli IKPU kod topadi.",
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Tovar nomi (masalan: shakar, un, telefon)",
                },
            },
        },
        handler=search_ikpu,
    )

    # ═══════════════════════════════════════
    # TO'LOV HAVOLASI (Payment links)
    # ═══════════════════════════════════════

    async def create_payment_link(params: dict) -> str:
        """Click yoki Payme to'lov havolasi yaratish."""
        system = params.get("system", "click").lower()
        amount = params.get("amount", 0)
        order_id = params.get("order_id", "1")
        description = params.get("description", "")

        if not amount or amount <= 0:
            return json.dumps({"error": "Summa kiritilishi shart (0 dan katta)"})

        if system == "click":
            merchant_id = params.get("merchant_id", "")
            service_id = params.get("service_id", "")
            if not merchant_id or not service_id:
                return json.dumps({"error": "Click uchun merchant_id va service_id kerak. Config da sozlang."})
            url = (
                f"https://my.click.uz/services/pay"
                f"?service_id={service_id}"
                f"&merchant_id={merchant_id}"
                f"&amount={amount}"
                f"&transaction_param={order_id}"
            )
            return json.dumps({
                "system": "Click",
                "url": url,
                "amount": amount,
                "amount_formatted": f"{amount:,.0f} so'm",
            }, ensure_ascii=False)

        elif system == "payme":
            merchant_id = params.get("merchant_id", "")
            if not merchant_id:
                return json.dumps({"error": "Payme uchun merchant_id kerak. Config da sozlang."})
            # Payme amount in tiyin (1 so'm = 100 tiyin)
            amount_tiyin = int(amount * 100)
            url = (
                f"https://checkout.paycom.uz/{merchant_id}"
                f"?a={amount_tiyin}"
                f"&ac.order_id={order_id}"
            )
            return json.dumps({
                "system": "Payme",
                "url": url,
                "amount": amount,
                "amount_formatted": f"{amount:,.0f} so'm",
            }, ensure_ascii=False)

        else:
            return json.dumps({"error": f"Noma'lum to'lov tizimi: {system}. click yoki payme tanlang."})

    registry.register(
        name="payment_link",
        description="Click yoki Payme to'lov havolasi yaratish. Mijozga yuborish uchun to'lov link.",
        parameters={
            "type": "object",
            "required": ["amount"],
            "properties": {
                "system": {
                    "type": "string",
                    "description": "To'lov tizimi: click yoki payme (default: click)",
                },
                "amount": {
                    "type": "number",
                    "description": "Summa (so'mda)",
                },
                "order_id": {
                    "type": "string",
                    "description": "Buyurtma ID (ixtiyoriy)",
                },
                "merchant_id": {
                    "type": "string",
                    "description": "Merchant ID (Click yoki Payme)",
                },
                "service_id": {
                    "type": "string",
                    "description": "Service ID (faqat Click uchun)",
                },
                "description": {
                    "type": "string",
                    "description": "To'lov tavsifi",
                },
            },
        },
        handler=create_payment_link,
    )

    # ═══════════════════════════════════════
    # KALKULYATORLAR (Calculators)
    # ═══════════════════════════════════════

    async def calculate_tax(params: dict) -> str:
        """QQS va soliq hisobi."""
        amount = params.get("amount", 0)
        if not amount:
            return json.dumps({"error": "Summa kiriting"})

        calc_type = params.get("type", "vat_add")
        vat_rate = params.get("vat_rate", 12)
        turnover_rate = params.get("turnover_rate", 4)

        result: dict[str, Any] = {"amount": amount}

        if calc_type == "vat_add":
            # QQS qo'shish: summa + 12%
            vat = round(amount * vat_rate / 100)
            result.update({
                "vat_rate": f"{vat_rate}%",
                "vat_amount": vat,
                "total": amount + vat,
                "description": f"{amount:,.0f} + QQS {vat_rate}% = {amount + vat:,.0f} so'm",
            })
        elif calc_type == "vat_extract":
            # QQS ajratish: summadan QQS ni ajratish
            vat = round(amount * vat_rate / (100 + vat_rate))
            net = amount - vat
            result.update({
                "vat_rate": f"{vat_rate}%",
                "vat_amount": vat,
                "net_amount": net,
                "description": f"{amount:,.0f} ichida QQS = {vat:,.0f} so'm, sof summa = {net:,.0f} so'm",
            })
        elif calc_type == "turnover":
            # Aylanma soliq
            tax = round(amount * turnover_rate / 100)
            result.update({
                "turnover_rate": f"{turnover_rate}%",
                "tax_amount": tax,
                "net_after_tax": amount - tax,
                "description": f"{amount:,.0f} dan aylanma soliq {turnover_rate}% = {tax:,.0f} so'm",
            })
        elif calc_type == "markup":
            # Ustama (markup)
            cost = params.get("cost", amount)
            markup_pct = params.get("markup", 30)
            sell_price = round(cost * (1 + markup_pct / 100))
            profit = sell_price - cost
            result.update({
                "cost": cost,
                "markup": f"{markup_pct}%",
                "sell_price": sell_price,
                "profit": profit,
                "description": f"Tan narxi {cost:,.0f} + {markup_pct}% = {sell_price:,.0f} so'm (foyda {profit:,.0f})",
            })
        elif calc_type == "installment":
            # Nasiya/bo'lib to'lash
            months = params.get("months", 12)
            interest = params.get("interest", 0)
            if interest > 0:
                monthly_rate = interest / 100 / 12
                payment = round(amount * monthly_rate / (1 - (1 + monthly_rate) ** -months))
            else:
                payment = round(amount / months)
            total = payment * months
            result.update({
                "months": months,
                "interest": f"{interest}%",
                "monthly_payment": payment,
                "total": total,
                "overpayment": total - amount,
                "description": f"{amount:,.0f} so'm / {months} oy = oyiga {payment:,.0f} so'm",
            })
        else:
            return json.dumps({"error": f"Noma'lum hisob turi: {calc_type}. vat_add, vat_extract, turnover, markup, installment dan birini tanlang."})

        return json.dumps(result, ensure_ascii=False)

    registry.register(
        name="tax_calculator",
        description="Soliq va biznes kalkulyator: QQS (12%), aylanma soliq (4%), ustama (markup), nasiya (bo'lib to'lash).",
        parameters={
            "type": "object",
            "required": ["amount"],
            "properties": {
                "amount": {"type": "number", "description": "Summa (so'mda)"},
                "type": {
                    "type": "string",
                    "description": "Hisob turi: vat_add (QQS qo'shish), vat_extract (QQS ajratish), turnover (aylanma soliq), markup (ustama), installment (nasiya)",
                },
                "cost": {"type": "number", "description": "Tan narxi (markup uchun)"},
                "markup": {"type": "number", "description": "Ustama foizi (default 30%)"},
                "months": {"type": "number", "description": "Oylar soni (nasiya uchun, default 12)"},
                "interest": {"type": "number", "description": "Yillik foiz stavkasi (nasiya uchun, default 0)"},
                "vat_rate": {"type": "number", "description": "QQS stavkasi (default 12%)"},
                "turnover_rate": {"type": "number", "description": "Aylanma soliq stavkasi (default 4%)"},
            },
        },
        handler=calculate_tax,
    )

    # ═══════════════════════════════════════
    # HUJJAT GENERATORI (Document templates)
    # ═══════════════════════════════════════

    async def generate_document(params: dict) -> str:
        """Biznes hujjat yaratish — shartnoma, faktura, dalolatnoma, ishonchnoma, talabnoma."""
        doc_type = params.get("type", "shartnoma")
        from datetime import datetime

        # Common fields
        company = params.get("company", "")
        inn = params.get("inn", "")
        director = params.get("director", "")
        address = params.get("address", "")
        bank = params.get("bank", "")
        account = params.get("account", "")
        mfo = params.get("mfo", "")
        counterparty = params.get("counterparty", "")
        counterparty_inn = params.get("counterparty_inn", "")
        counterparty_director = params.get("counterparty_director", "")
        counterparty_address = params.get("counterparty_address", "")
        counterparty_bank = params.get("counterparty_bank", "")
        counterparty_account = params.get("counterparty_account", "")
        counterparty_mfo = params.get("counterparty_mfo", "")
        amount = params.get("amount", 0)
        description = params.get("description", "")
        date = params.get("date", datetime.now().strftime("%d.%m.%Y"))
        number = params.get("number", "1")
        city = params.get("city", "Toshkent")

        if not company or not counterparty:
            return json.dumps({"error": "company va counterparty kiritilishi shart"})

        amount_str = f"{amount:,.0f}" if amount else "___________"
        blank = "_______________"

        def rekvizit(name, stir, addr, bnk, acc, mf, dir_name):
            return (
                f"{name}\n"
                f"Manzil: {addr or blank}\n"
                f"STIR: {stir or blank}\n"
                f"H/r: {acc or blank}\n"
                f"Bank: {bnk or blank}\n"
                f"MFO: {mf or blank}\n"
                f"Rahbar: {dir_name or blank}\n"
                f"\n_______________ / {dir_name or blank} /\n"
                f"      M.O."
            )

        if doc_type == "shartnoma":
            valid_until = params.get("valid_until", blank)
            content = (
                f"SHARTNOMA No {number}\n\n"
                f"{city} shahri{' ' * 40}{date}\n\n"
                f"{company} (keyingi o'rinlarda \"Buyurtmachi\" deb yuritiladi), "
                f"STIR {inn or blank}, rahbar {director or blank} shaxsida, "
                f"bir tomondan, va\n\n"
                f"{counterparty} (keyingi o'rinlarda \"Bajaruvchi\" deb yuritiladi), "
                f"STIR {counterparty_inn or blank}, rahbar {counterparty_director or blank} shaxsida, "
                f"ikkinchi tomondan,\n\n"
                f"quyidagilar to'g'risida mazkur shartnomani tuzdilar:\n\n"
                f"1. SHARTNOMA PREDMETI\n\n"
                f"1.1. Bajaruvchi quyidagi ish/xizmatlarni bajarishni o'z zimmasiga oladi:\n"
                f"{description or blank}\n\n"
                f"1.2. Shartnomaning umumiy qiymati {amount_str} (so'm) ni tashkil etadi.\n\n"
                f"2. TOMONLARNING HUQUQ VA MAJBURIYATLARI\n\n"
                f"2.1. Bajaruvchi majburiyatlari:\n"
                f"  a) ishni sifatli va belgilangan muddatda bajarish;\n"
                f"  b) Buyurtmachiga ish haqida o'z vaqtida xabar berish;\n"
                f"  c) bajarilgan ish natijalarini topshirish.\n\n"
                f"2.2. Buyurtmachi majburiyatlari:\n"
                f"  a) ish uchun zarur sharoitlarni yaratish;\n"
                f"  b) to'lovni belgilangan muddatda amalga oshirish;\n"
                f"  c) bajarilgan ishni qabul qilish.\n\n"
                f"3. NARX VA TO'LOV TARTIBI\n\n"
                f"3.1. To'lov bank o'tkazmasi orqali amalga oshiriladi.\n"
                f"3.2. To'lov muddati: dalolatnoma imzolanganidan keyin 5 (besh) bank kuni ichida.\n\n"
                f"4. JAVOBGARLIK\n\n"
                f"4.1. Tomonlar o'z majburiyatlarini bajarmagan yoki lozim darajada bajarmagan "
                f"taqdirda O'zR Fuqarolik Kodeksiga muvofiq javobgar bo'ladilar.\n"
                f"4.2. Kechiktirilgan har bir kun uchun penya: shartnoma summasining 0,5% "
                f"miqdorida, lekin 50% dan oshmasligi kerak.\n\n"
                f"5. NIZOLARNI HAL QILISH TARTIBI\n\n"
                f"5.1. Tomonlar o'rtasidagi nizolar muzokaralar yo'li bilan hal qilinadi.\n"
                f"5.2. Kelishuvga erishilmagan taqdirda nizo iqtisodiy sudda ko'riladi.\n"
                f"5.3. Sudgacha tartib (talabnoma) majburiydir.\n\n"
                f"6. SHARTNOMA MUDDATI\n\n"
                f"6.1. Shartnoma imzolangan kundan boshlab kuchga kiradi.\n"
                f"6.2. Shartnoma muddati: {valid_until} gacha.\n\n"
                f"7. YAKUNIY QOIDALAR\n\n"
                f"7.1. Shartnomaga o'zgartirish faqat tomonlarning yozma kelishuviga asosan kiritiladi.\n"
                f"7.2. Shartnoma 2 (ikki) nusxada tuzilgan, har bir tomon uchun bittadan.\n"
                f"7.3. Shartnomada ko'rsatilmagan masalalar O'zR amaldagi qonunchiligiga muvofiq tartibga solinadi.\n\n"
                f"8. TOMONLARNING REKVIZITLARI VA IMZOLARI\n\n"
                f"Buyurtmachi:\n{rekvizit(company, inn, address, bank, account, mfo, director)}\n\n"
                f"Bajaruvchi:\n{rekvizit(counterparty, counterparty_inn, counterparty_address, counterparty_bank, counterparty_account, counterparty_mfo, counterparty_director)}"
            )

        elif doc_type == "faktura":
            items = params.get("items", [])
            items_text = ""
            total = 0
            for i, item in enumerate(items, 1):
                name = item.get("name", "")
                qty = item.get("quantity", 1)
                unit = item.get("unit", "dona")
                price = item.get("price", 0)
                summa = qty * price
                vat_item = round(summa * 12 / 100)
                total += summa
                items_text += f"  {i}. {name} | {unit} | {qty} | {price:,.0f} | {summa:,.0f} | 12% | {vat_item:,.0f} | {summa + vat_item:,.0f}\n"

            if not items_text:
                total = amount

            vat_total = round(total * 12 / 100)
            contract_ref = params.get("contract_number", blank)
            contract_date = params.get("contract_date", blank)

            content = (
                f"HISOBVARAQ-FAKTURA No {number}\n"
                f"Sana: {date}\n\n"
                f"Shartnoma: No {contract_ref} sanasi {contract_date}\n\n"
                f"SOTUVCHI (yetkazib beruvchi):\n"
                f"  Nomi: {company}\n  Manzili: {address or blank}\n"
                f"  STIR: {inn or blank}\n  H/r: {account or blank}\n"
                f"  Bank: {bank or blank}\n  MFO: {mfo or blank}\n\n"
                f"XARIDOR (oluvchi):\n"
                f"  Nomi: {counterparty}\n  Manzili: {counterparty_address or blank}\n"
                f"  STIR: {counterparty_inn or blank}\n  H/r: {counterparty_account or blank}\n"
                f"  Bank: {counterparty_bank or blank}\n  MFO: {counterparty_mfo or blank}\n\n"
                f"No | Tovar nomi | Birlik | Miqdor | Narx | Qiymat (QQSsiz) | QQS % | QQS summa | Qiymat (QQS bilan)\n"
                f"{'=' * 90}\n"
                f"{items_text}"
                f"{'=' * 90}\n"
                f"  Jami (QQSsiz): {total:,.0f} so'm\n"
                f"  QQS (12%): {vat_total:,.0f} so'm\n"
                f"  JAMI (QQS bilan): {total + vat_total:,.0f} so'm\n\n"
                f"Rahbar: _______________ / {director or blank} /\n"
                f"Bosh hisobchi: _______________ / {blank} /\n"
                f"M.O."
            )

        elif doc_type == "dalolatnoma":
            contract_ref = params.get("contract_number", blank)
            contract_date = params.get("contract_date", blank)
            period_from = params.get("period_from", blank)
            period_to = params.get("period_to", blank)

            content = (
                f"BAJARILGAN ISHLAR (KO'RSATILGAN XIZMATLAR) DALOLATNOMASI No {number}\n\n"
                f"Sana: {date}\n"
                f"Shartnoma asosi: No {contract_ref} sanasi {contract_date}\n"
                f"Ish bajarilgan davr: {period_from} dan {period_to} gacha\n\n"
                f"Buyurtmachi: {company}, STIR {inn or blank}\n"
                f"Bajaruvchi: {counterparty}, STIR {counterparty_inn or blank}\n\n"
                f"BAJARILGAN ISHLAR:\n\n"
                f"{description or blank}\n\n"
                f"Ish/xizmat qiymati: {amount_str} so'm\n"
                f"QQS (12%): {round(amount * 12 / 100):,.0f} so'm\n" if amount else ""
                f"Jami: {round(amount * 1.12):,.0f} so'm\n\n" if amount else "\n"
                f"Ish sifati bo'yicha Buyurtmachining da'volari: YO'Q\n\n"
                f"Buyurtmachi:\n"
                f"{company}\n"
                f"Rahbar: _______________ / {director or blank} /\n"
                f"M.O.\n\n"
                f"Bajaruvchi:\n"
                f"{counterparty}\n"
                f"Rahbar: _______________ / {counterparty_director or blank} /\n"
                f"M.O."
            )

        elif doc_type == "ishonchnoma":
            person = params.get("person", "")
            passport = params.get("passport", "")
            position = params.get("position", "")
            purpose = params.get("purpose", description)
            valid_until = params.get("valid_until", blank)
            supplier = params.get("supplier", counterparty)

            content = (
                f"ISHONCHNOMA No {number}\n"
                f"(Tovar-moddiy boyliklarni olishga)\n\n"
                f"Berilgan sana: {date}\n"
                f"Haqiqiylik muddati: {valid_until} gacha\n\n"
                f"Korxona: {company}\n"
                f"Manzil: {address or blank}\n"
                f"STIR: {inn or blank}\n\n"
                f"Ishonchnoma berildi:\n"
                f"  F.I.Sh.: {person or blank}\n"
                f"  Lavozimi: {position or blank}\n"
                f"  Pasport: {passport or blank}\n\n"
                f"Mol yetkazib beruvchi: {supplier}\n"
                f"Hujjat asosi: {purpose or f'Shartnoma No {blank}'}\n\n"
                f"Olinadigan tovar-moddiy boyliklar:\n\n"
                f"  No | Nomi | O'lchov birligi | Soni (yozuv bilan)\n"
                f"  {'=' * 60}\n"
                f"  1. {description or blank}\n\n"
                f"Ishonchnomani olgan shaxsning imzosi: _______________\n\n"
                f"Tasdiqlash:\n"
                f"Rahbar: _______________ / {director or blank} /\n"
                f"M.O."
            )

        elif doc_type == "talabnoma":
            debt_amount = params.get("debt_amount", amount)
            penalty_rate = params.get("penalty_rate", 0.5)
            days_overdue = params.get("days_overdue", 0)
            penalty = round(debt_amount * penalty_rate / 100 * days_overdue) if days_overdue else 0
            total_claim = debt_amount + penalty
            deadline = params.get("deadline", "10 (o'n) kun")
            contract_ref = params.get("contract_number", blank)
            contract_date = params.get("contract_date", blank)

            content = (
                f"TALABNOMA No {number}\n"
                f"(Muddati o'tgan debitor qarzdorlikni to'lash to'g'risida)\n\n"
                f"Sana: {date}\n\n"
                f"Kimga: {counterparty}\n"
                f"Manzil: {counterparty_address or blank}\n"
                f"Rahbar: {counterparty_director or blank}\n\n"
                f"Kimdan: {company}\n"
                f"Manzil: {address or blank}\n"
                f"STIR: {inn or blank}\n\n"
                f"Hurmatli {counterparty_director or 'rahbar'}!\n\n"
                f"Sizning tashkilotingiz bilan tuzilgan No {contract_ref} sanasi {contract_date} "
                f"shartnomaga asosan, Siz quyidagi majburiyatni o'z vaqtida bajarmadingiz:\n\n"
                f"{description or blank}\n\n"
                f"Asosiy qarz summasi: {debt_amount:,.0f} so'm\n"
            )
            if penalty:
                content += (
                    f"Kechiktirilgan kunlar: {days_overdue}\n"
                    f"Penya ({penalty_rate}% kuniga): {penalty:,.0f} so'm\n"
                )
            content += (
                f"JAMI TALAB: {total_claim:,.0f} so'm\n\n"
                f"Huquqiy asos: O'zR Fuqarolik Kodeksining 333, 334-moddalari.\n\n"
                f"TALAB QILAMIZ:\n\n"
                f"Yuqoridagi summani {deadline} ichida quyidagi rekvizitlarga o'tkazishingizni:\n"
                f"  H/r: {account or blank}\n"
                f"  Bank: {bank or blank}\n"
                f"  MFO: {mfo or blank}\n\n"
                f"Aks holda, O'zR Iqtisodiy Protsessual Kodeksiga muvofiq "
                f"iqtisodiy sudga da'vo arizasi bilan murojaat qilamiz.\n\n"
                f"Ilovalar:\n"
                f"  1. Shartnoma nusxasi\n"
                f"  2. Hisobvaraq-faktura nusxasi\n"
                f"  3. Qarz hisob-kitobi\n\n"
                f"Rahbar: _______________ / {director or blank} /\n"
                f"M.O."
            )
        else:
            return json.dumps({"error": f"Noma'lum hujjat turi: {doc_type}. shartnoma, faktura, dalolatnoma, ishonchnoma, talabnoma dan birini tanlang."})

        # Return content for agent to use with create_docx or send directly
        return json.dumps({
            "type": doc_type,
            "content": content,
            "filename": f"{doc_type}_{number}_{date.replace('.', '-')}.txt",
            "hint": "Bu matnni create_docx tool bilan DOCX faylga saqlash mumkin, yoki to'g'ridan-to'g'ri foydalanuvchiga ko'rsatish mumkin.",
        }, ensure_ascii=False)

    registry.register(
        name="generate_document",
        description="Rasmiy biznes hujjat yaratish: shartnoma, hisob-faktura, dalolatnoma, ishonchnoma, talabnoma. O'zR qonunchiligiga mos.",
        parameters={
            "type": "object",
            "required": ["type", "company", "counterparty"],
            "properties": {
                "type": {"type": "string", "description": "Hujjat turi: shartnoma, faktura, dalolatnoma, ishonchnoma, talabnoma"},
                "company": {"type": "string", "description": "Kompaniya nomi"},
                "inn": {"type": "string", "description": "STIR (INN)"},
                "director": {"type": "string", "description": "Rahbar F.I.Sh."},
                "address": {"type": "string", "description": "Manzil"},
                "bank": {"type": "string", "description": "Bank nomi"},
                "account": {"type": "string", "description": "Hisob raqam"},
                "mfo": {"type": "string", "description": "MFO"},
                "counterparty": {"type": "string", "description": "Kontragent nomi"},
                "counterparty_inn": {"type": "string", "description": "Kontragent STIR"},
                "counterparty_director": {"type": "string", "description": "Kontragent rahbar F.I.Sh."},
                "counterparty_address": {"type": "string", "description": "Kontragent manzil"},
                "counterparty_bank": {"type": "string", "description": "Kontragent bank"},
                "counterparty_account": {"type": "string", "description": "Kontragent hisob raqam"},
                "counterparty_mfo": {"type": "string", "description": "Kontragent MFO"},
                "amount": {"type": "number", "description": "Summa (so'mda)"},
                "description": {"type": "string", "description": "Ish/xizmat/tovar tavsifi"},
                "number": {"type": "string", "description": "Hujjat raqami"},
                "date": {"type": "string", "description": "Sana (DD.MM.YYYY)"},
                "city": {"type": "string", "description": "Shahar (default: Toshkent)"},
                "valid_until": {"type": "string", "description": "Muddat (shartnoma/ishonchnoma uchun)"},
                "contract_number": {"type": "string", "description": "Shartnoma raqami (faktura/dalolatnoma uchun)"},
                "contract_date": {"type": "string", "description": "Shartnoma sanasi"},
                "items": {"type": "array", "description": "Tovarlar (faktura uchun): [{name, quantity, unit, price}]",
                          "items": {"type": "object", "properties": {"name": {"type": "string"}, "quantity": {"type": "number"}, "unit": {"type": "string"}, "price": {"type": "number"}}}},
                "person": {"type": "string", "description": "Ishonchnoma oluvchi F.I.Sh."},
                "passport": {"type": "string", "description": "Pasport"},
                "position": {"type": "string", "description": "Lavozim"},
                "debt_amount": {"type": "number", "description": "Qarz summasi (talabnoma uchun)"},
                "penalty_rate": {"type": "number", "description": "Penya stavkasi % kuniga (default 0.5)"},
                "days_overdue": {"type": "number", "description": "Kechiktirilgan kunlar soni"},
                "deadline": {"type": "string", "description": "To'lov muddati (default: 10 kun)"},
            },
        },
        handler=generate_document,
    )

    # ═══════════════════════════════════════
    # OB-HAVO (Weather)
    # ═══════════════════════════════════════

    async def get_weather(params: dict) -> str:
        """Ob-havo ma'lumoti."""
        city = params.get("city", "Tashkent")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://wttr.in/{city}?format=j1",
                    headers={"Accept-Language": "uz"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)

            # wttr.in wraps everything under "data" key
            inner = data.get("data", data)
            current = inner.get("current_condition", [{}])[0]
            today = inner.get("weather", [{}])[0]
            tomorrow = inner.get("weather", [{}, {}])[1] if len(inner.get("weather", [])) > 1 else {}

            result = {
                "city": city,
                "now": {
                    "temp": f"{current.get('temp_C', '?')}°C",
                    "feels_like": f"{current.get('FeelsLikeC', '?')}°C",
                    "condition": current.get("lang_uz", [{}])[0].get("value", current.get("weatherDesc", [{}])[0].get("value", "")) if current.get("lang_uz") else current.get("weatherDesc", [{}])[0].get("value", ""),
                    "humidity": f"{current.get('humidity', '?')}%",
                    "wind": f"{current.get('windspeedKmph', '?')} km/s",
                },
                "today": {
                    "max": f"{today.get('maxtempC', '?')}°C",
                    "min": f"{today.get('mintempC', '?')}°C",
                },
            }

            if tomorrow:
                result["tomorrow"] = {
                    "max": f"{tomorrow.get('maxtempC', '?')}°C",
                    "min": f"{tomorrow.get('mintempC', '?')}°C",
                }

            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"Ob-havo olishda xatolik: {e}"})

    registry.register(
        name="weather",
        description="Ob-havo ma'lumoti — bugungi va ertangi havo, harorat, shamol, namlik.",
        parameters={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "Shahar nomi (default: Tashkent). Masalan: Samarkand, Bukhara, Namangan",
                },
            },
        },
        handler=get_weather,
    )

    logger.info("Local tools registered: currency_rate, ikpu_search, payment_link, tax_calculator, generate_document, weather")
