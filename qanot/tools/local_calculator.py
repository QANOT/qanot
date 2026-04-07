"""Soliq va biznes kalkulyatorlar."""

from __future__ import annotations

import json
import logging
from typing import Any

from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)


def register_calculator_tools(registry: ToolRegistry) -> None:
    """Register tax and business calculator tools."""

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
        description="Tax and business calculator: VAT (12%), turnover tax (4%), markup, installment payments.",
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
