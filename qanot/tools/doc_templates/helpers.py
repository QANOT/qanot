"""Shared helpers for document templates."""

from __future__ import annotations

from datetime import datetime

BLANK = "_______________"


def _rekvizit(name: str, stir: str, addr: str, bnk: str, acc: str, mf: str, dir_name: str) -> str:
    """Standart rekvizitlar bloki."""
    return (
        f"{name}\n"
        f"Manzil: {addr or BLANK}\n"
        f"STIR: {stir or BLANK}\n"
        f"H/r: {acc or BLANK}\n"
        f"Bank: {bnk or BLANK}\n"
        f"MFO: {mf or BLANK}\n"
        f"Rahbar: {dir_name or BLANK}\n"
        f"\n_______________ / {dir_name or BLANK} /\n"
        f"      M.O."
    )


def _amount_str(amount: float | int) -> str:
    return f"{amount:,.0f}" if amount else BLANK


def _common_fields(params: dict) -> dict:
    """Extract common fields from params."""
    return {
        "company": params.get("company", ""),
        "inn": params.get("inn", ""),
        "director": params.get("director", ""),
        "address": params.get("address", ""),
        "bank": params.get("bank", ""),
        "account": params.get("account", ""),
        "mfo": params.get("mfo", ""),
        "counterparty": params.get("counterparty", ""),
        "counterparty_inn": params.get("counterparty_inn", ""),
        "counterparty_director": params.get("counterparty_director", ""),
        "counterparty_address": params.get("counterparty_address", ""),
        "counterparty_bank": params.get("counterparty_bank", ""),
        "counterparty_account": params.get("counterparty_account", ""),
        "counterparty_mfo": params.get("counterparty_mfo", ""),
        "amount": params.get("amount", 0),
        "description": params.get("description", ""),
        "date": params.get("date", datetime.now().strftime("%d.%m.%Y")),
        "number": params.get("number", "1"),
        "city": params.get("city", "Toshkent"),
    }
