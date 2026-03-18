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


def register_uzbek_tools(registry: ToolRegistry) -> None:
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

    logger.info("Uzbek business tools registered: currency_rate, ikpu_search, payment_link, tax_calculator")


# Note: IKPU API (tasnif.soliq.uz) requires BearerToken auth.
# Current implementation tries public endpoint which may not work.
# TODO: Add token-based auth when IKPU credentials are available.
