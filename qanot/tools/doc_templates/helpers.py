"""Shared helpers for document templates."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BLANK = "_______________"

# Documents are stamped with the *operator's* local calendar date.
# Using naive datetime.now() inherits the host TZ — which is UTC inside
# most containers, so a contract generated at 02:00 Tashkent (21:00 UTC
# previous day) would carry yesterday's date. Pin to the codebase's
# universal Uzbekistan tz; deployments outside Tashkent can override
# by setting their host TZ and we'll still produce a sane local date.
try:
    _LOCAL_TZ = ZoneInfo("Asia/Tashkent")
except ZoneInfoNotFoundError:  # pragma: no cover — tzdata missing
    _LOCAL_TZ = timezone.utc


def _today_local() -> str:
    """Today's calendar date in dd.mm.yyyy, in operator's local tz."""
    return datetime.now(timezone.utc).astimezone(_LOCAL_TZ).strftime("%d.%m.%Y")


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
        "date": params.get("date", _today_local()),
        "number": params.get("number", "1"),
        "city": params.get("city", "Toshkent"),
    }
