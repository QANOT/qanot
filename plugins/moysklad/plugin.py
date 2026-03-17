"""MoySklad (МойСклад) plugin — tovarlar, ombor, sotuvlar, xaridlar, hisobotlar."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from qanot.plugins.base import Plugin, ToolDef

logger = logging.getLogger(__name__)

TOOLS_MD = (Path(__file__).parent / "TOOLS.md").read_text(encoding="utf-8") if (Path(__file__).parent / "TOOLS.md").exists() else ""
SOUL_APPEND = (Path(__file__).parent / "SOUL_APPEND.md").read_text(encoding="utf-8") if (Path(__file__).parent / "SOUL_APPEND.md").exists() else ""

BASE_URL = "https://api.moysklad.ru/api/remap/1.2"


class MoySkladClient:
    """HTTP client for MoySklad API with Basic Auth."""

    def __init__(self, login: str, password: str):
        creds = base64.b64encode(f"{login}:{password}".encode()).decode()
        self._auth_header = f"Basic {creds}"
        self._session: aiohttp.ClientSession | None = None
        self._rate_limiter = asyncio.Semaphore(4)  # max 5 parallel, keep 1 reserve

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def test_auth(self) -> bool:
        """Test credentials by fetching current employee."""
        try:
            await self.get("entity/employee", params={"limit": "1"})
            return True
        except Exception as e:
            logger.error("[moysklad] Auth test failed: %s", e)
            return False

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: Any = None) -> Any:
        return await self._request("POST", path, body=body)

    async def _request(self, method: str, path: str,
                       body: Any = None, params: dict | None = None) -> Any:
        async with self._rate_limiter:
            session = await self._get_session()
            url = f"{BASE_URL}/{path.lstrip('/')}"
            headers = {
                "Authorization": self._auth_header,
                "Accept-Encoding": "gzip",
                "Content-Type": "application/json",
            }
            clean_params = {k: str(v) for k, v in (params or {}).items()
                           if v is not None and v != ""} or None

            async with session.request(
                method, url,
                json=body if method != "GET" else None,
                params=clean_params,
                headers=headers,
            ) as resp:
                if resp.status == 401:
                    raise RuntimeError("MoySklad autentifikatsiya xatosi — login/parol noto'g'ri")
                if resp.status == 403:
                    raise RuntimeError("Ruxsat etilmagan")
                if resp.status == 404:
                    raise RuntimeError(f"Topilmadi: {path}")
                if resp.status == 429:
                    retry = resp.headers.get("X-Lognex-Retry-TimeInterval", "3000")
                    raise RuntimeError(f"Juda ko'p so'rov — {int(retry)//1000}s kuting")
                if resp.status >= 500:
                    raise RuntimeError(f"MoySklad server xatosi ({resp.status})")

                data = await resp.json(content_type=None)

                if isinstance(data, dict) and "errors" in data:
                    err = data["errors"][0] if data["errors"] else {}
                    raise RuntimeError(err.get("error", "MoySklad xatosi"))

                return data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class QanotPlugin(Plugin):
    """MoySklad ombor boshqaruv tizimi plugin."""

    name = "moysklad"
    description = "MoySklad — tovarlar, ombor qoldig'i, sotuvlar, xaridlar, hisobotlar"
    tools_md = TOOLS_MD
    soul_append = SOUL_APPEND

    def __init__(self):
        self.client: MoySkladClient | None = None

    async def setup(self, config: dict) -> None:
        login = config.get("login", "")
        password = config.get("password", "")
        if not all([login, password]):
            logger.warning("[moysklad] Missing config (login, password)")
            return
        self.client = MoySkladClient(login=login, password=password)
        if not await self.client.test_auth():
            logger.error("[moysklad] Auth failed — plugin disabled")
            self.client = None
            return
        logger.info("[moysklad] Plugin ready")

    async def teardown(self) -> None:
        if self.client:
            await self.client.close()

    def get_tools(self) -> list[ToolDef]:
        if not self.client:
            return []
        tools = self._build_api_tools()
        logger.info("[moysklad] %d tools registered", len(tools))
        return tools

    def _ok(self, data: Any) -> str:
        # Simplify response — extract rows and meta
        if isinstance(data, dict) and "rows" in data:
            result: dict[str, Any] = {"data": data["rows"]}
            meta = data.get("meta", {})
            if "size" in meta:
                result["total"] = meta["size"]
                result["offset"] = meta.get("offset", 0)
                result["limit"] = meta.get("limit", 1000)
            return json.dumps(result, indent=2, ensure_ascii=False)
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _err(self, msg: str) -> str:
        return json.dumps({"error": msg})

    def _build_api_tools(self) -> list[ToolDef]:
        c = self.client
        assert c is not None
        tools: list[ToolDef] = []

        def _paginated(name: str, desc: str, path: str, extra_params: dict):
            base_props = {
                "limit": {"type": "number", "description": "Natijalar soni (max 1000, default 25)"},
                "offset": {"type": "number", "description": "Boshlang'ich pozitsiya (default 0)"},
            }
            base_props.update(extra_params)

            async def handler(p: dict, _path=path) -> str:
                try:
                    params: dict[str, Any] = {"limit": p.get("limit", 25)}
                    if p.get("offset"):
                        params["offset"] = p["offset"]
                    if p.get("search"):
                        params["search"] = p["search"]
                    # Build filter string
                    filters = []
                    for k, v in p.items():
                        if k not in ("limit", "offset", "search") and v is not None and v != "":
                            filters.append(f"{k}={v}")
                    if filters:
                        params["filter"] = ";".join(filters)
                    return self._ok(await c.get(_path, params))
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc,
                                 parameters={"type": "object", "properties": base_props},
                                 handler=handler))

        def _get_by_id(name: str, desc: str, path: str):
            async def handler(p: dict, _path=path) -> str:
                try:
                    return self._ok(await c.get(f"{_path}/{p['id']}"))
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc,
                                 parameters={"type": "object", "required": ["id"],
                                              "properties": {"id": {"type": "string", "description": "ID (UUID)"}}},
                                 handler=handler))

        def _simple_get(name: str, desc: str, path: str):
            async def handler(p: dict, _path=path) -> str:
                try:
                    params: dict[str, Any] = {"limit": p.get("limit", 25)}
                    if p.get("offset"):
                        params["offset"] = p["offset"]
                    return self._ok(await c.get(_path, params))
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc,
                                 parameters={"type": "object", "properties": {
                                     "limit": {"type": "number", "description": "Natijalar soni (default 25)"},
                                     "offset": {"type": "number", "description": "Offset"},
                                 }}, handler=handler))

        def _report(name: str, desc: str, path: str, extra_params: dict | None = None):
            props = {
                "limit": {"type": "number", "description": "Natijalar soni (default 25)"},
                "offset": {"type": "number", "description": "Offset"},
            }
            if extra_params:
                props.update(extra_params)

            async def handler(p: dict, _path=path) -> str:
                try:
                    params: dict[str, Any] = {"limit": p.get("limit", 25)}
                    if p.get("offset"):
                        params["offset"] = p["offset"]
                    if p.get("momentFrom"):
                        params["momentFrom"] = p["momentFrom"]
                    if p.get("momentTo"):
                        params["momentTo"] = p["momentTo"]
                    return self._ok(await c.get(_path, params))
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc,
                                 parameters={"type": "object", "properties": props},
                                 handler=handler))

        # ═══════════════════════════════════════
        # TOVARLAR (Products) — 5 tools
        # ═══════════════════════════════════════
        _paginated("ms_search_products", "Tovarlar qidirish. Nomi, artikul bo'yicha.",
                   "entity/product", {
                       "search": {"type": "string", "description": "Tovar nomi yoki artikul"},
                   })

        _get_by_id("ms_get_product", "Bitta tovar tafsilotlari (ID bo'yicha).", "entity/product")

        _paginated("ms_get_assortment", "Yagona katalog — tovarlar, xizmatlar, variantlar, komplektlar.",
                   "entity/assortment", {
                       "search": {"type": "string", "description": "Qidirish"},
                   })

        _simple_get("ms_get_product_folders", "Tovar kategoriyalari (papkalar).", "entity/productfolder")

        _simple_get("ms_get_currencies", "Valyutalar ro'yxati.", "entity/currency")

        # ═══════════════════════════════════════
        # OMBOR / QOLDIQ (Stock) — 3 tools
        # ═══════════════════════════════════════
        _report("ms_get_stock", "Tovarlar qoldig'i — barcha ombordagi mavjud miqdor va narx.",
                "report/stock/all", {
                    "search": {"type": "string", "description": "Tovar nomi bo'yicha qidirish"},
                })

        _report("ms_get_stock_by_store", "Ombor bo'yicha qoldiq — har bir ombordagi tovarlar.",
                "report/stock/bystore")

        _simple_get("ms_get_stores", "Omborlar ro'yxati.", "entity/store")

        # ═══════════════════════════════════════
        # KONTRAGENTLAR (Counterparties) — 3 tools
        # ═══════════════════════════════════════
        _paginated("ms_search_counterparties", "Mijoz va ta'minotchilar qidirish.",
                   "entity/counterparty", {
                       "search": {"type": "string", "description": "Ism, kompaniya yoki telefon"},
                   })

        _get_by_id("ms_get_counterparty", "Bitta kontragent tafsilotlari.", "entity/counterparty")

        _report("ms_counterparty_report", "Kontragentlar hisoboti — sotuvlar, qarz, o'rtacha chek.",
                "report/counterparty")

        # ═══════════════════════════════════════
        # SOTUVLAR (Sales/Orders) — 5 tools
        # ═══════════════════════════════════════
        _paginated("ms_get_customer_orders", "Buyurtmalar ro'yxati. Sana, kontragent bo'yicha filter.",
                   "entity/customerorder", {
                       "search": {"type": "string", "description": "Qidirish"},
                   })

        _get_by_id("ms_get_customer_order", "Bitta buyurtma tafsilotlari.", "entity/customerorder")

        _paginated("ms_get_demands", "Sotuvlar (jo'natmalar) ro'yxati.",
                   "entity/demand", {
                       "search": {"type": "string", "description": "Qidirish"},
                   })

        _paginated("ms_get_sales_returns", "Qaytarilgan sotuvlar.", "entity/salesreturn", {})

        async def get_sales_plot(p: dict) -> str:
            try:
                params: dict[str, Any] = {
                    "momentFrom": p.get("momentFrom", ""),
                    "momentTo": p.get("momentTo", ""),
                    "interval": p.get("interval", "day"),
                }
                return self._ok(await c.get("report/sales/plotseries", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("ms_sales_chart", "Sotuv grafigi — vaqt bo'yicha sotuv hajmi.",
                             {"type": "object", "required": ["momentFrom", "momentTo"], "properties": {
                                 "momentFrom": {"type": "string", "description": "Boshlanish (YYYY-MM-DD HH:mm:ss)"},
                                 "momentTo": {"type": "string", "description": "Tugash (YYYY-MM-DD HH:mm:ss)"},
                                 "interval": {"type": "string", "description": "Interval: hour, day, month (default: day)"},
                             }}, get_sales_plot))

        # ═══════════════════════════════════════
        # XARIDLAR (Purchases) — 3 tools
        # ═══════════════════════════════════════
        _paginated("ms_get_purchase_orders", "Xarid buyurtmalari.", "entity/purchaseorder", {
            "search": {"type": "string", "description": "Qidirish"},
        })

        _paginated("ms_get_supplies", "Kirimlar (ta'minotdan qabul).", "entity/supply", {
            "search": {"type": "string", "description": "Qidirish"},
        })

        _paginated("ms_get_purchase_returns", "Qaytarilgan xaridlar.", "entity/purchasereturn", {})

        # ═══════════════════════════════════════
        # TO'LOVLAR (Payments) — 4 tools
        # ═══════════════════════════════════════
        _paginated("ms_get_payments_in", "Kiruvchi to'lovlar (mijozlardan).", "entity/paymentin", {})
        _paginated("ms_get_payments_out", "Chiquvchi to'lovlar (ta'minotchilarga).", "entity/paymentout", {})
        _paginated("ms_get_invoices_out", "Chiquvchi hisob-fakturalar (mijozlarga).", "entity/invoiceout", {})
        _paginated("ms_get_invoices_in", "Kiruvchi hisob-fakturalar (ta'minotchilardan).", "entity/invoicein", {})

        # ═══════════════════════════════════════
        # HISOBOTLAR (Reports) — 5 tools
        # ═══════════════════════════════════════
        _report("ms_profit_by_product", "Tovar bo'yicha rentabellik — foyda, marja, sotuv summasi.",
                "report/profit/byproduct", {
                    "momentFrom": {"type": "string", "description": "Boshlanish (YYYY-MM-DD HH:mm:ss)"},
                    "momentTo": {"type": "string", "description": "Tugash (YYYY-MM-DD HH:mm:ss)"},
                })

        _report("ms_profit_by_counterparty", "Kontragent bo'yicha rentabellik.",
                "report/profit/bycounterparty", {
                    "momentFrom": {"type": "string", "description": "Boshlanish"},
                    "momentTo": {"type": "string", "description": "Tugash"},
                })

        _report("ms_turnover", "Tovar aylanmasi hisoboti.", "report/turnover/all", {
            "momentFrom": {"type": "string", "description": "Boshlanish"},
            "momentTo": {"type": "string", "description": "Tugash"},
        })

        async def get_cash_flow(p: dict) -> str:
            try:
                params: dict[str, Any] = {
                    "momentFrom": p.get("momentFrom", ""),
                    "momentTo": p.get("momentTo", ""),
                    "interval": p.get("interval", "day"),
                }
                return self._ok(await c.get("report/money/plotseries", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("ms_cash_flow", "Pul oqimi — kirim, chiqim, balans vaqt bo'yicha.",
                             {"type": "object", "required": ["momentFrom", "momentTo"], "properties": {
                                 "momentFrom": {"type": "string", "description": "Boshlanish (YYYY-MM-DD HH:mm:ss)"},
                                 "momentTo": {"type": "string", "description": "Tugash (YYYY-MM-DD HH:mm:ss)"},
                                 "interval": {"type": "string", "description": "Interval: hour, day, month"},
                             }}, get_cash_flow))

        async def get_orders_plot(p: dict) -> str:
            try:
                params: dict[str, Any] = {
                    "momentFrom": p.get("momentFrom", ""),
                    "momentTo": p.get("momentTo", ""),
                    "interval": p.get("interval", "day"),
                }
                return self._ok(await c.get("report/orders/plotseries", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("ms_orders_chart", "Buyurtmalar grafigi — vaqt bo'yicha buyurtma hajmi.",
                             {"type": "object", "required": ["momentFrom", "momentTo"], "properties": {
                                 "momentFrom": {"type": "string", "description": "Boshlanish (YYYY-MM-DD HH:mm:ss)"},
                                 "momentTo": {"type": "string", "description": "Tugash (YYYY-MM-DD HH:mm:ss)"},
                                 "interval": {"type": "string", "description": "Interval: hour, day, month"},
                             }}, get_orders_plot))

        # ═══════════════════════════════════════
        # TASHKILOT (Organization) — 2 tools
        # ═══════════════════════════════════════
        _simple_get("ms_get_organizations", "Yuridik shaxslar ro'yxati.", "entity/organization")
        _simple_get("ms_get_employees", "Xodimlar ro'yxati.", "entity/employee")

        return tools
