"""Scanner plugin — phone-photo documents → structured data → user's chosen format.

Architecture: thin plugin. Claude vision does extraction natively in the agent
loop, core tools (sheets_*, create_docx, create_xlsx, create_pdf) handle output.
This plugin contributes:
  - SOUL_APPEND.md: a detailed prompt teaching the agent the extract→save flow
    for 9 doctypes (receipt, invoice, business card, contract, menu, handwritten,
    product catalog, ID document, order form).
  - 3 helper tools: scanner_doctypes (reference), expense_summary (aggregation),
    expense_categorize (vendor→category heuristic).

The plugin has no config keys — works for any bot where it's enabled. It relies
on the sheets plugin being connected for most output paths (the SOUL tells the
agent how to degrade gracefully when it isn't).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from qanot.plugins.base import Plugin, ToolDef, tool

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(PLUGIN_DIR))


class ScannerPlugin(Plugin):
    """Document scanner — receipt, invoice, business card, menu, contract, …"""

    name = "scanner"
    description = "Phone-photo documents → structured data → Sheet/Excel/PDF/Word/CRM"
    version = "0.1.0"

    def __init__(self) -> None:
        pass

    async def setup(self, config: dict) -> None:
        logger.info("Scanner plugin ready — 9 doctypes, 3 tools")

    async def teardown(self) -> None:
        pass

    def get_tools(self) -> list[ToolDef]:
        return self._collect_tools()

    # ── Tools ────────────────────────────────────────────────────

    @tool(
        name="scanner_doctypes",
        description=(
            "List the document types this scanner knows how to extract and route. "
            "Call this ONCE when a photo arrives and you're unsure which schema "
            "to use. Returns a list of doctypes with: key, Uzbek names the user "
            "might say, fields to extract, default output format (sheet/xlsx/pdf/"
            "docx/crm), and notes on special handling."
        ),
        parameters={"type": "object", "properties": {}},
    )
    async def scanner_doctypes(self, params: dict) -> str:
        from engine.doctypes import as_dict_list

        return json.dumps(
            {"status": "ok", "doctypes": as_dict_list()},
            ensure_ascii=False,
        )

    @tool(
        name="expense_categorize",
        description=(
            "Classify an expense into one of 14 Uzbek business categories "
            "(Oziq-ovqat, Restoran, Transport, Yoqilg'i, Kommunal, Ijara, Maosh, "
            "Tovar, Tibbiyot, Ta'lim, Reklama, Ofis, Texnika, Boshqa). Uses "
            "regex-based keyword match against common Uzbek vendor names. "
            "Returns null if no confident match — in that case, classify "
            "yourself based on context OR ask the user."
        ),
        parameters={
            "type": "object",
            "required": ["vendor"],
            "properties": {
                "vendor": {"type": "string", "description": "Vendor/shop name from the receipt"},
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional item descriptions from the receipt",
                },
            },
        },
    )
    async def expense_categorize(self, params: dict) -> str:
        from engine.categorize import CATEGORIES, categorize

        vendor = (params.get("vendor") or "").strip()
        items = params.get("items") or []
        if not isinstance(items, list):
            items = []

        result = categorize(vendor, [str(i) for i in items])
        return json.dumps(
            {
                "status": "ok",
                "vendor": vendor,
                "category": result,
                "matched": result is not None,
                "valid_categories": list(CATEGORIES),
                "hint": (
                    None
                    if result
                    else "Keyword match failed. Pick a category based on vendor/items "
                    "context, or ask the user. Valid values in valid_categories."
                ),
            },
            ensure_ascii=False,
        )

    @tool(
        name="expense_summary",
        description=(
            "Summarize expenses over a period. Reads the Xarajatlar sheet "
            "(or another expense sheet you specify), aggregates by currency + "
            "category, and returns total, top transactions, and period-over-"
            "period delta. Answers questions like 'bu oyda qancha xarajat "
            "qildim?', 'o'tgan hafta oziq-ovqatga qancha ketdi?'.\n\n"
            "PRE-REQ: the sheets plugin must be connected. First call "
            "sheets_read to fetch the raw rows, then pass them here as "
            "`rows`. This tool is pure aggregation — it does not hit any API."
        ),
        parameters={
            "type": "object",
            "required": ["period", "rows"],
            "properties": {
                "period": {
                    "type": "string",
                    "description": (
                        "today | yesterday | week | month | year | "
                        "YYYY-MM-DD..YYYY-MM-DD"
                    ),
                },
                "rows": {
                    "type": "array",
                    "description": (
                        "Raw rows from sheets_read including header row. "
                        "Schema: [Sana, Do'kon, Summa, Valyuta, Kategoriya, "
                        "Izoh, Mahsulotlar]."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Optional: restrict to one category "
                        "(e.g. 'Oziq-ovqat')."
                    ),
                },
                "prev_rows": {
                    "type": "array",
                    "description": (
                        "Optional: rows covering the previous window for "
                        "delta reporting. If omitted, no delta shown."
                    ),
                },
                "top_n": {
                    "type": "integer",
                    "description": "Max top-transactions to return (default 5)",
                },
            },
        },
    )
    async def expense_summary(self, params: dict) -> str:
        from engine.summarize import (
            PeriodError,
            parse_period,
            summarize,
            summary_to_markdown,
        )

        period_str = params.get("period") or "month"
        rows = params.get("rows") or []
        prev_rows = params.get("prev_rows")
        category = params.get("category")
        try:
            top_n = int(params.get("top_n") or 5)
        except (TypeError, ValueError):
            top_n = 5
        top_n = max(1, min(20, top_n))

        if not isinstance(rows, list):
            return json.dumps(
                {"status": "error", "error": "rows must be a 2D array"},
                ensure_ascii=False,
            )

        try:
            period = parse_period(period_str)
        except PeriodError as e:
            return json.dumps(
                {"status": "error", "error": str(e)}, ensure_ascii=False,
            )

        try:
            summary = summarize(
                rows,
                period,
                prev_rows=prev_rows if isinstance(prev_rows, list) else None,
                category=category,
                top_n=top_n,
            )
        except Exception as e:
            logger.exception("expense_summary failed")
            return json.dumps(
                {"status": "error", "error": f"Aggregation failed: {e}"},
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "status": "ok",
                "period": {
                    "start": summary.period.start.isoformat(),
                    "end": summary.period.end.isoformat(),
                    "days": summary.period.days,
                },
                "entry_count": summary.entry_count,
                "total_by_currency": summary.total_by_currency,
                "by_category": summary.by_category,
                "top_transactions": summary.top_transactions,
                "prev_total_by_currency": summary.prev_total_by_currency,
                "markdown": summary_to_markdown(summary),
            },
            ensure_ascii=False,
        )
