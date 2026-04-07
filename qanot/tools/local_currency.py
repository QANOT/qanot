"""Valyuta kursi — CBU rasmiy kurslari."""

from __future__ import annotations

import json
import logging

import aiohttp

from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)

# CBU API — Markaziy bank rasmiy kurslari (bepul, autentifikatsiyasiz)
CBU_URL = "https://cbu.uz/ru/arkhiv-kursov-valyut/json/"


def register_currency_tools(registry: ToolRegistry) -> None:
    """Register currency rate tools."""

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
        description="Get today's official currency exchange rates from CBU (Central Bank of Uzbekistan). USD, EUR, RUB, and more.",
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
