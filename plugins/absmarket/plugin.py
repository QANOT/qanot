"""AbsMarket POS plugin — 30 API tools + MySQL query."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from qanot.plugins.base import Plugin, ToolDef

logger = logging.getLogger(__name__)

TOOLS_MD = (Path(__file__).parent / "TOOLS.md").read_text(encoding="utf-8") if (Path(__file__).parent / "TOOLS.md").exists() else ""
SOUL_APPEND = (Path(__file__).parent / "SOUL_APPEND.md").read_text(encoding="utf-8") if (Path(__file__).parent / "SOUL_APPEND.md").exists() else ""


class AbsMarketClient:
    """HTTP client for AbsMarket POS API."""

    def __init__(self, base_url: str, email: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.token: str | None = None
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def login(self) -> None:
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/api/v2/auth/login",
            json={"email_address": self.email, "password": self.password},
            headers={"Content-Type": "application/json"},
        ) as resp:
            data = await resp.json()
        token = None
        if isinstance(data, dict):
            token = data.get("data", {}).get("token") if isinstance(data.get("data"), dict) else None
            if not token and isinstance(data.get("data"), dict):
                nested = data["data"].get("data", {})
                if isinstance(nested, dict):
                    token = nested.get("token")
        if not token:
            raise RuntimeError(f"Login failed: {json.dumps(data)[:200]}")
        self.token = token

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def get_all(self, path: str, data_key: str, params: dict | None = None) -> dict:
        """Fetch all pages of a paginated endpoint."""
        all_items: list = []
        page = 1
        limit = 100
        total = 0
        while True:
            p = {**(params or {}), "page": page, "limit": limit}
            data = await self.get(path, p)
            items = data.get(data_key, []) if isinstance(data, dict) else []
            all_items.extend(items)
            pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
            if pagination:
                total = pagination.get("total", total)
            if not pagination or page >= pagination.get("pages", 1):
                break
            page += 1
        return {"items": all_items, "total": total or len(all_items)}

    async def _request(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any:
        data = await self._raw(method, path, body, params)
        if isinstance(data, dict) and not data.get("success", True):
            msg = data.get("message", "")
            if any(kw in str(msg).lower() for kw in ["authorization token", "unauthorized", "expired token"]):
                await self.login()
                return (await self._raw(method, path, body, params)).get("data", {})
            raise RuntimeError(msg or "API error")
        return data.get("data", data) if isinstance(data, dict) else data

    async def _raw(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # Build query string
        clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None and v != ""}
        async with session.request(method, url, json=body if method != "GET" else None, params=clean_params, headers=headers) as resp:
            return await resp.json()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class QanotPlugin(Plugin):
    """AbsMarket POS plugin."""

    name = "absmarket"
    description = "AbsMarket POS — sotuvlar, xaridlar, mijozlar, tovarlar"
    tools_md = TOOLS_MD
    soul_append = SOUL_APPEND

    def __init__(self):
        self.client: AbsMarketClient | None = None
        self._db_pool = None
        self._db_config: dict = {}
        self._workspace_dir: str = ""

    async def setup(self, config: dict) -> None:
        api_url = config.get("api_url", "")
        email = config.get("email", "")
        password = config.get("password", "")
        # workspace_dir is injected by qanot/plugins/loader.py so plugins can
        # spool large outputs to disk for the agent (avoids inline payloads
        # blowing the agent context).
        self._workspace_dir = config.get("workspace_dir", "")
        if not api_url or not email or not password:
            logger.warning("[absmarket] Missing config (api_url, email, password)")
            return
        self.client = AbsMarketClient(api_url, email, password)
        try:
            await self.client.login()
            logger.info("[absmarket] Logged in successfully")
        except Exception as e:
            logger.error("[absmarket] Login failed: %s", e)
            self.client = None

        # DB config
        self._db_config = {
            "host": config.get("db_host", ""),
            "port": int(config.get("db_port", 3306)),
            "user": config.get("db_user", ""),
            "password": config.get("db_password", ""),
            "db": config.get("db_name", "absmarket"),
        }
        if self._db_config["host"] and self._db_config["user"] and self._db_config["password"]:
            try:
                import aiomysql
                self._db_pool = await aiomysql.create_pool(
                    host=self._db_config["host"],
                    port=self._db_config["port"],
                    user=self._db_config["user"],
                    password=self._db_config["password"],
                    db=self._db_config["db"],
                    maxsize=2,
                    connect_timeout=5,
                )
                logger.info("[absmarket] DB pool created")
            except Exception as e:
                logger.warning("[absmarket] DB pool failed: %s", e)

    async def teardown(self) -> None:
        if self.client:
            await self.client.close()
        if self._db_pool:
            self._db_pool.close()
            await self._db_pool.wait_closed()

    def get_tools(self) -> list[ToolDef]:
        if not self.client:
            return []
        tools = self._build_api_tools()
        if self._db_pool:
            tools.append(self._build_query_tool())
        logger.info("[absmarket] %d tools registered", len(tools))
        return tools

    def _ok(self, data: Any) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _err(self, msg: str) -> str:
        return json.dumps({"error": msg})

    def _build_api_tools(self) -> list[ToolDef]:
        c = self.client
        assert c is not None

        tools: list[ToolDef] = []

        def _simple(name: str, desc: str, path: str, params_schema: dict, path_param: str | None = None):
            async def handler(p: dict, _path=path, _pk=path_param) -> str:
                try:
                    actual_path = _path
                    if _pk and _pk in p:
                        actual_path = _path.replace(f"{{{_pk}}}", str(p.pop(_pk)))
                    return self._ok(await c.get(actual_path, p if p else None))
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc, parameters=params_schema, handler=handler))

        # ── SALES ──
        _simple("absmarket_get_sales", "Sotuvlar ro'yxati. Sana, mijoz, do'kon bo'yicha filter.", "/api/v2/sales", {
            "type": "object", "properties": {
                "from_date": {"type": "string", "description": "Boshlanish sanasi (YYYY-MM-DD)"},
                "to_date": {"type": "string", "description": "Tugash sanasi (YYYY-MM-DD)"},
                "customer_id": {"type": "number", "description": "Mijoz ID"},
                "outlet_id": {"type": "number", "description": "Do'kon ID"},
                "page": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absmarket_get_sale_details", "Bitta sotuv tafsilotlari.", "/api/v2/sales/{sale_id}", {
            "type": "object", "required": ["sale_id"], "properties": {"sale_id": {"type": "number"}}}, "sale_id")
        _simple("absmarket_get_recent_sales", "So'nggi sotuvlar.", "/api/v2/sales/recent", {
            "type": "object", "properties": {"limit": {"type": "number"}}})

        # Sales summary (aggregation)
        async def sales_summary(p: dict) -> str:
            try:
                result = await c.get_all("/api/v2/sales", "sales", p)
                sales = result["items"]
                total_payable = sum(s.get("total_payable", 0) for s in sales)
                total_paid = sum(s.get("paid_amount", 0) for s in sales)
                total_due = sum(s.get("due_amount", 0) for s in sales)
                total_discount = sum(s.get("total_discount", 0) for s in sales)
                by_outlet: dict = {}
                for s in sales:
                    outlet = s.get("outlet_name") or s.get("outlet_id") or "Noma'lum"
                    by_outlet.setdefault(outlet, {"count": 0, "total": 0})
                    by_outlet[outlet]["count"] += 1
                    by_outlet[outlet]["total"] += s.get("total_payable", 0)
                return self._ok({
                    "jami_sotuvlar_soni": result["total"],
                    "jami_summa": total_payable, "jami_tolangan": total_paid,
                    "jami_qarz": total_due, "jami_chegirma": total_discount,
                    "dokonlar_boyicha": by_outlet,
                })
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("absmarket_get_sales_summary",
            "Sotuvlar umumiy hisoboti — BARCHA sahifalarni o'qib jami summani hisoblaydi.",
            {"type": "object", "properties": {
                "from_date": {"type": "string"}, "to_date": {"type": "string"},
                "customer_id": {"type": "number"}, "outlet_id": {"type": "number"},
            }}, sales_summary))

        # Purchases summary
        async def purchases_summary(p: dict) -> str:
            try:
                result = await c.get_all("/api/v2/purchases", "purchases", p)
                purchases = result["items"]
                total_amount = sum(x.get("total_amount", 0) or x.get("grand_total", 0) for x in purchases)
                total_paid = sum(x.get("paid_amount", 0) for x in purchases)
                total_due = sum(x.get("due_amount", 0) for x in purchases)
                return self._ok({
                    "jami_xaridlar_soni": result["total"],
                    "jami_summa": total_amount, "jami_tolangan": total_paid, "jami_qarz": total_due,
                })
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("absmarket_get_purchases_summary",
            "Xaridlar umumiy hisoboti.", {"type": "object", "properties": {
                "date_from": {"type": "string"}, "date_to": {"type": "string"},
                "supplier_id": {"type": "number"},
            }}, purchases_summary))

        # ── PURCHASES ──
        _simple("absmarket_get_purchases", "Xaridlar ro'yxati.", "/api/v2/purchases", {
            "type": "object", "properties": {
                "date_from": {"type": "string"}, "date_to": {"type": "string"},
                "supplier_id": {"type": "number"}, "page": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absmarket_get_purchase_details", "Bitta xarid tafsilotlari.", "/api/v2/purchases/{purchase_id}", {
            "type": "object", "required": ["purchase_id"], "properties": {"purchase_id": {"type": "number"}}}, "purchase_id")
        _simple("absmarket_get_recent_purchases", "So'nggi xaridlar.", "/api/v2/purchases/recent", {
            "type": "object", "properties": {"limit": {"type": "number"}}})

        # ── EXPENSES ──
        _simple("absmarket_get_expenses", "Xarajatlar ro'yxati.", "/api/v2/expenses", {
            "type": "object", "properties": {
                "start_date": {"type": "string"}, "end_date": {"type": "string"},
                "category_id": {"type": "number"}, "employee_id": {"type": "number"},
                "page": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absmarket_get_expense_details", "Bitta xarajat tafsilotlari.", "/api/v2/expenses/{expense_id}", {
            "type": "object", "required": ["expense_id"], "properties": {"expense_id": {"type": "number"}}}, "expense_id")
        _simple("absmarket_get_expense_categories", "Xarajat kategoriyalari.", "/api/v2/expense-categories", {
            "type": "object", "properties": {}})

        # ── CUSTOMERS ──
        _simple("absmarket_get_customers", "Mijozlar ro'yxati yoki qidirish.", "/api/v2/customers", {
            "type": "object", "properties": {
                "search": {"type": "string"}, "page": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absmarket_get_customer_details", "Mijoz ma'lumotlari.", "/api/v2/customers/{customer_id}", {
            "type": "object", "required": ["customer_id"], "properties": {"customer_id": {"type": "number"}}}, "customer_id")

        # Customer balance (needs path param extraction)
        async def customer_balance(p: dict) -> str:
            try:
                cid = p.pop("customer_id")
                return self._ok(await c.get(f"/api/v2/customers/{cid}/balance", p if p else None))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("absmarket_get_customer_balance", "Mijoz balansi.", {
            "type": "object", "required": ["customer_id"], "properties": {
                "customer_id": {"type": "number"}, "outlet_id": {"type": "number"},
            }}, customer_balance))

        async def customer_history(p: dict) -> str:
            try:
                cid = p.pop("customer_id")
                return self._ok(await c.get(f"/api/v2/customers/{cid}/history", p))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("absmarket_get_customer_history", "Mijoz tranzaksiya tarixi.", {
            "type": "object", "required": ["customer_id", "from", "to"], "properties": {
                "customer_id": {"type": "number"}, "from": {"type": "string"}, "to": {"type": "string"},
                "outlet_id": {"type": "number"},
            }}, customer_history))

        _simple("absmarket_get_customer_payments", "Mijozlardan to'lovlar.", "/api/v2/payments/customers", {
            "type": "object", "properties": {
                "customer_id": {"type": "number"}, "page": {"type": "number"}, "limit": {"type": "number"},
            }})

        # ── SUPPLIERS ──
        _simple("absmarket_get_suppliers", "Ta'minotchilar ro'yxati.", "/api/v2/suppliers", {
            "type": "object", "properties": {
                "search": {"type": "string"}, "page": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absmarket_get_supplier_details", "Ta'minotchi ma'lumotlari.", "/api/v2/suppliers/{supplier_id}", {
            "type": "object", "required": ["supplier_id"], "properties": {"supplier_id": {"type": "number"}}}, "supplier_id")

        async def supplier_balance(p: dict) -> str:
            try:
                sid = p.pop("supplier_id")
                return self._ok(await c.get(f"/api/v2/suppliers/{sid}/balance", p if p else None))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("absmarket_get_supplier_balance", "Ta'minotchi balansi.", {
            "type": "object", "required": ["supplier_id"], "properties": {
                "supplier_id": {"type": "number"}, "currency": {"type": "string"},
            }}, supplier_balance))

        _simple("absmarket_get_supplier_payments", "Ta'minotchilarga to'lovlar.", "/api/v2/payments/suppliers", {
            "type": "object", "properties": {
                "supplier_id": {"type": "number"}, "page": {"type": "number"}, "limit": {"type": "number"},
            }})

        # ── ITEMS ──
        _simple("absmarket_get_items", "Tovarlar ro'yxati.", "/api/v2/items", {
            "type": "object", "properties": {
                "search": {"type": "string"}, "category_id": {"type": "number"},
                "brand_id": {"type": "number"}, "type": {"type": "string"},
                "page": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absmarket_get_item_details", "Tovar tafsilotlari.", "/api/v2/items/{item_id}", {
            "type": "object", "required": ["item_id"], "properties": {"item_id": {"type": "number"}}}, "item_id")

        async def item_stock(p: dict) -> str:
            try:
                iid = p.pop("item_id")
                return self._ok(await c.get(f"/api/v2/items/{iid}/stock", {"outlet_id": p.get("outlet_id")}))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("absmarket_get_item_stock", "Tovar qoldig'i.", {
            "type": "object", "required": ["item_id", "outlet_id"], "properties": {
                "item_id": {"type": "number"}, "outlet_id": {"type": "number"},
            }}, item_stock))

        _simple("absmarket_get_item_categories", "Tovar kategoriyalari.", "/api/v2/items/categories", {
            "type": "object", "properties": {}})

        # ── OUTLETS ──
        _simple("absmarket_get_outlets", "Do'konlar ro'yxati.", "/api/v2/outlets", {
            "type": "object", "properties": {}})

        # ── RETURNS ──
        _simple("absmarket_get_sale_returns", "Sotuv qaytarishlari.", "/api/v2/sale-returns", {
            "type": "object", "properties": {
                "customer_id": {"type": "number"}, "page": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absmarket_get_sale_return_details", "Sotuv qaytarish tafsilotlari.", "/api/v2/sale-returns/{return_id}", {
            "type": "object", "required": ["return_id"], "properties": {"return_id": {"type": "number"}}}, "return_id")
        _simple("absmarket_get_purchase_returns", "Xarid qaytarishlari.", "/api/v2/purchase-returns", {
            "type": "object", "properties": {
                "supplier_id": {"type": "number"}, "page": {"type": "number"}, "limit": {"type": "number"},
            }})

        # ── STOCK & TRANSFERS ──
        _simple("absmarket_get_stock_adjustments", "Ombor tuzatishlari.", "/api/v2/stock-adjustments", {
            "type": "object", "properties": {
                "item_id": {"type": "number"}, "adjustment_type": {"type": "string"},
                "start_date": {"type": "string"}, "end_date": {"type": "string"},
                "page": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absmarket_get_transfers", "Do'konlar orasidagi ko'chirishlar.", "/api/v2/transfers", {
            "type": "object", "properties": {
                "from_outlet_id": {"type": "number"}, "to_outlet_id": {"type": "number"},
                "status": {"type": "number"}, "page": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absmarket_get_transfer_details", "Ko'chirish tafsilotlari.", "/api/v2/transfers/{transfer_id}", {
            "type": "object", "required": ["transfer_id"], "properties": {"transfer_id": {"type": "number"}}}, "transfer_id")

        return tools

    def _build_query_tool(self) -> ToolDef:
        """Build the MySQL query tool."""
        import re
        import time

        # When the result has more rows than this, return a preview + spool the
        # full set to a file in the workspace so the agent can either send it
        # directly (Excel) or process it without burning iterations on
        # paginated re-queries. Hard upper cap keeps memory bounded for very
        # large queries (1M rows of 50 cols would be ~100 MB JSON).
        INLINE_THRESHOLD = 500
        HARD_ROW_CAP = 100_000

        async def absmarket_query(p: dict) -> str:
            query = (p.get("query", "") or "").strip()
            output_format = (p.get("output_format") or "auto").lower()
            if output_format not in ("auto", "json", "xlsx"):
                return self._err("output_format faqat: auto | json | xlsx")
            if not re.match(r"^SELECT\b", query, re.IGNORECASE):
                return self._err("Faqat SELECT so'rovlar ruxsat etilgan.")
            blocked = re.compile(
                r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|EXEC|CALL)\b",
                re.IGNORECASE,
            )
            if blocked.search(query):
                return self._err("Bu turdagi so'rov ruxsat etilmagan.")
            try:
                async with self._db_pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("SET SESSION MAX_EXECUTION_TIME = 30000")
                        await cur.execute(query)
                        rows = await cur.fetchall()
                        cols = [d[0] for d in cur.description] if cur.description else []
                        result_rows = [dict(zip(cols, row)) for row in rows]
                        total = len(result_rows)
                        capped = total > HARD_ROW_CAP
                        if capped:
                            result_rows = result_rows[:HARD_ROW_CAP]

                        # Resolve format: when "auto", pick xlsx for large
                        # results so the agent can `send_file` directly without
                        # re-querying or running a Python script.
                        fmt = output_format
                        if fmt == "auto":
                            fmt = "xlsx" if total > INLINE_THRESHOLD else "json"

                        # Small result + json: keep current inline contract.
                        if fmt == "json" and total <= INLINE_THRESHOLD:
                            return self._ok({
                                "rows": result_rows,
                                "total_rows": total,
                                "truncated": False,
                            })

                        # Large result OR explicit xlsx — spool to workspace.
                        out_dir = Path(self._workspace_dir or ".") / "generated"
                        out_dir.mkdir(parents=True, exist_ok=True)
                        ts = int(time.time())

                        if fmt == "xlsx":
                            fname = f"absmarket_query_{ts}.xlsx"
                            path = out_dir / fname
                            self._write_xlsx(path, cols, result_rows)
                        else:
                            fname = f"absmarket_query_{ts}.json"
                            path = out_dir / fname
                            path.write_text(
                                json.dumps(
                                    {
                                        "columns": cols,
                                        "total_rows": total,
                                        "rows": result_rows,
                                    },
                                    ensure_ascii=False,
                                    default=str,
                                ),
                                encoding="utf-8",
                            )

                        preview = result_rows[: min(20, total)]
                        return self._ok({
                            "format": fmt,
                            "preview": preview,
                            "total_rows": total,
                            "row_cap_applied": capped,
                            "file_path": str(path),
                            "advice": (
                                "Excel/JSON foyli to'liq natija bilan saqlandi. "
                                "Foydalanuvchiga yuborish uchun: send_file({path}). "
                                "Qayta absmarket_query chaqirib paginatsiya QILMANG."
                            ),
                        })
            except Exception as e:
                return self._err(str(e))

        return ToolDef(
            name="absmarket_query",
            description=(
                "MySQL bazaga SELECT so'rov yuborish. Faqat SELECT ruxsat etilgan. "
                "Asosiy jadvallar: tbl_sales, tbl_sales_details, tbl_items, tbl_item_categories, "
                "tbl_customers, tbl_suppliers, tbl_purchase, tbl_expenses. "
                "del_status='Live' filtrini qo'shish SHART. "
                "500+ qator natija bo'lsa avtomatik Excel fayl sifatida saqlanadi: "
                "preview + file_path qaytadi, agent send_file orqali yuboradi. "
                "Pagination qo'lda kerak EMAS."
            ),
            parameters={"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string", "description": "SQL SELECT so'rov"},
                "output_format": {
                    "type": "string",
                    "enum": ["auto", "json", "xlsx"],
                    "description": (
                        "auto (default): kichik natija inline JSON, katta — Excel fayli. "
                        "json: har doim JSON fayl. xlsx: har doim Excel fayl."
                    ),
                },
            }},
            handler=absmarket_query,
        )

    @staticmethod
    def _write_xlsx(path: Path, cols: list[str], rows: list[dict]) -> None:
        """Write rows as Excel via openpyxl (already a project dep)."""
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = "absmarket_query"
        ws.append(cols)
        for r in rows:
            ws.append([r.get(c) for c in cols])
        # Auto-size columns up to a sane max so the file is usable without
        # manual fiddling. Sample first 50 rows for width estimation.
        for idx, col in enumerate(cols, start=1):
            sample = [str(r.get(col, "")) for r in rows[:50]]
            width = min(max([len(col)] + [len(s) for s in sample]), 60)
            ws.column_dimensions[get_column_letter(idx)].width = width + 2
        wb.save(path)
