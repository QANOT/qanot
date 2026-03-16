"""ibox.io inventory management plugin — tovarlar, ombor, sotuvlar, hisobotlar."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from qanot.plugins.base import Plugin, ToolDef

logger = logging.getLogger(__name__)

TOOLS_MD = (Path(__file__).parent / "TOOLS.md").read_text(encoding="utf-8") if (Path(__file__).parent / "TOOLS.md").exists() else ""
SOUL_APPEND = (Path(__file__).parent / "SOUL_APPEND.md").read_text(encoding="utf-8") if (Path(__file__).parent / "SOUL_APPEND.md").exists() else ""


class IboxClient:
    """HTTP client for ibox.io API with auto-login and token refresh."""

    def __init__(self, tenant: str, login: str, password: str,
                 filial_id: int | None = None, currency_id: int = 1):
        self.base_url = f"https://{tenant}.ibox.io/api"
        self.login_creds = {"login": login, "password": password}
        self.token: str | None = None
        self.filial_id: int = filial_id or 1
        self.currency_id = currency_id
        self.filials: list[dict] = []
        self._session: aiohttp.ClientSession | None = None
        self._rate_limiter = asyncio.Semaphore(5)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def authenticate(self) -> bool:
        """Login and get token + filials."""
        try:
            session = await self._get_session()
            async with session.post(
                f"{self.base_url}/user/login",
                json=self.login_creds,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("[ibox] Login failed (%d): %s", resp.status, text[:200])
                    return False
                data = await resp.json()
                self.token = data.get("token")
                self.filials = data.get("filials", [])
                if self.filials and self.filial_id == 1:
                    self.filial_id = self.filials[0].get("id", 1)
                logger.info("[ibox] Authenticated — %d filial(s), filial_id=%d",
                            len(self.filials), self.filial_id)
                return True
        except Exception as e:
            logger.error("[ibox] Login error: %s", e)
            return False

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: Any = None) -> Any:
        return await self._request("POST", path, body=body)

    async def put(self, path: str, body: Any = None) -> Any:
        return await self._request("PUT", path, body=body)

    async def _request(self, method: str, path: str,
                       body: Any = None, params: dict | None = None,
                       _retry: bool = True) -> Any:
        async with self._rate_limiter:
            session = await self._get_session()
            url = f"{self.base_url}/{path.lstrip('/')}"
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Filial-Id": str(self.filial_id),
            }
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"

            clean_params = {k: str(v) for k, v in (params or {}).items()
                           if v is not None and v != ""} or None

            async with session.request(
                method, url,
                json=body if method != "GET" else None,
                params=clean_params,
                headers=headers,
            ) as resp:
                if resp.status == 401 and _retry:
                    if await self.authenticate():
                        return await self._request(method, path, body, params, _retry=False)
                    raise RuntimeError("ibox autentifikatsiya xatosi")

                if resp.status == 403:
                    raise RuntimeError("Ruxsat etilmagan")
                if resp.status == 404:
                    raise RuntimeError(f"Topilmadi: {path}")
                if resp.status == 429:
                    raise RuntimeError("Juda ko'p so'rov — biroz kuting")
                if resp.status >= 500:
                    raise RuntimeError(f"ibox server xatosi ({resp.status})")

                if resp.content_type == "application/json":
                    data = await resp.json()
                elif resp.status == 204:
                    return {}
                else:
                    text = await resp.text()
                    if "<html" in text.lower():
                        raise RuntimeError("ibox API javob bermadi (HTML qaytardi)")
                    return {"_raw": text}

                if isinstance(data, dict) and "message" in data and resp.status >= 400:
                    raise RuntimeError(data["message"])
                return data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class QanotPlugin(Plugin):
    """ibox.io ombor boshqaruv tizimi plugin."""

    name = "ibox"
    description = "ibox.io — tovarlar, ombor qoldig'i, sotuvlar, xaridlar, hisobotlar"
    tools_md = TOOLS_MD
    soul_append = SOUL_APPEND

    def __init__(self):
        self.client: IboxClient | None = None

    async def setup(self, config: dict) -> None:
        tenant = config.get("tenant", "")
        login = config.get("login", "")
        password = config.get("password", "")
        if not all([tenant, login, password]):
            logger.warning("[ibox] Missing config (tenant, login, password)")
            return
        self.client = IboxClient(
            tenant=tenant,
            login=login,
            password=password,
            filial_id=config.get("filial_id"),
            currency_id=config.get("currency_id", 1),
        )
        if not await self.client.authenticate():
            logger.error("[ibox] Initial login failed — plugin disabled")
            self.client = None
            return
        logger.info("[ibox] Plugin ready — tenant=%s", tenant)

    async def teardown(self) -> None:
        if self.client:
            await self.client.close()

    def get_tools(self) -> list[ToolDef]:
        if not self.client:
            return []
        tools = self._build_api_tools()
        logger.info("[ibox] %d tools registered", len(tools))
        return tools

    def _ok(self, data: Any) -> str:
        if isinstance(data, dict) and "data" in data:
            result: dict[str, Any] = {"data": data["data"]}
            if "current_page" in data:
                result["page"] = data["current_page"]
                result["total"] = data.get("total", 0)
                result["last_page"] = data.get("last_page", 1)
            return json.dumps(result, indent=2, ensure_ascii=False)
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _err(self, msg: str) -> str:
        return json.dumps({"error": msg})

    def _build_api_tools(self) -> list[ToolDef]:
        c = self.client
        assert c is not None
        tools: list[ToolDef] = []

        # ── Helper: paginated GET ──
        def _paginated(name: str, desc: str, path: str, extra_params: dict):
            base_props = {
                "page": {"type": "number", "description": "Sahifa raqami (1 dan boshlanadi)"},
                "limit": {"type": "number", "description": "Natijalar soni (default 20)"},
            }
            base_props.update(extra_params)

            async def handler(p: dict, _path=path) -> str:
                try:
                    params: dict[str, Any] = {"per_page": p.get("limit", 20)}
                    if p.get("page"):
                        params["page"] = p["page"]
                    for k, v in p.items():
                        if k not in ("page", "limit") and v is not None and v != "":
                            params[k] = v
                    return self._ok(await c.get(_path, params))
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc,
                                 parameters={"type": "object", "properties": base_props},
                                 handler=handler))

        def _simple_get(name: str, desc: str, path: str):
            async def handler(p: dict, _path=path) -> str:
                try:
                    return self._ok(await c.get(_path))
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc,
                                 parameters={"type": "object", "properties": {}},
                                 handler=handler))

        def _get_by_id(name: str, desc: str, path: str):
            async def handler(p: dict, _path=path) -> str:
                try:
                    return self._ok(await c.get(f"{_path}/{p['id']}"))
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc,
                                 parameters={"type": "object", "required": ["id"],
                                              "properties": {"id": {"type": "number", "description": "ID"}}},
                                 handler=handler))

        # ═══════════════════════════════════════
        # TOVARLAR (Products) — 5 tools
        # ═══════════════════════════════════════
        _paginated("ibox_search_products", "Tovarlar ro'yxati. Nomi, shtrix-kod, SKU bo'yicha qidirish.",
                   "product/product", {
                       "search": {"type": "string", "description": "Tovar nomi, shtrix-kod yoki SKU"},
                       "category_id": {"type": "number", "description": "Kategoriya ID"},
                       "brand_id": {"type": "number", "description": "Brend ID"},
                       "active": {"type": "boolean", "description": "Faqat faol tovarlar"},
                   })

        _get_by_id("ibox_get_product", "Bitta tovar tafsilotlari (ID bo'yicha).", "product/product")

        _simple_get("ibox_get_categories", "Tovar kategoriyalari ro'yxati.", "product/category")

        _simple_get("ibox_get_brands", "Tovar brendlari ro'yxati.", "product/brand")

        _simple_get("ibox_get_units", "O'lchov birliklari (dona, kg, litr).", "product/unit")

        # ═══════════════════════════════════════
        # OMBOR / QOLDIQ (Stock) — 4 tools
        # ═══════════════════════════════════════
        _paginated("ibox_get_stock", "Ombordagi tovarlar qoldig'i. Tovar, ombor, kategoriya bo'yicha filter.",
                   "report/stock", {
                       "search": {"type": "string", "description": "Tovar nomi bo'yicha qidirish"},
                       "warehouse_id": {"type": "number", "description": "Ombor ID bo'yicha filter"},
                       "category_id": {"type": "number", "description": "Kategoriya bo'yicha filter"},
                   })

        async def get_stock_by_product(p: dict) -> str:
            try:
                params = {"product_id": p["product_id"]}
                if p.get("date"):
                    params["date"] = p["date"]
                return self._ok(await c.get("document/stock/by-product", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("ibox_get_stock_by_product",
                             "Bitta tovar bo'yicha barcha ombordagi qoldiq.",
                             {"type": "object", "required": ["product_id"], "properties": {
                                 "product_id": {"type": "number", "description": "Tovar ID"},
                                 "date": {"type": "string", "description": "Sana (yyyy-MM-dd), default bugun"},
                             }}, get_stock_by_product))

        _simple_get("ibox_get_warehouses", "Omborlar ro'yxati.", "core/warehouse")

        async def get_stock_by_warehouse(p: dict) -> str:
            try:
                params: dict[str, Any] = {"warehouse_id": p["warehouse_id"]}
                if p.get("date"):
                    params["date"] = p["date"]
                return self._ok(await c.get("document/stock/by-warehouse", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("ibox_get_stock_by_warehouse",
                             "Bitta ombor bo'yicha barcha tovarlar qoldig'i.",
                             {"type": "object", "required": ["warehouse_id"], "properties": {
                                 "warehouse_id": {"type": "number", "description": "Ombor ID"},
                                 "date": {"type": "string", "description": "Sana (yyyy-MM-dd)"},
                             }}, get_stock_by_warehouse))

        # ═══════════════════════════════════════
        # SOTUVLAR (Sales/Orders) — 4 tools
        # ═══════════════════════════════════════
        _paginated("ibox_get_orders", "Buyurtmalar (sotuvlar) ro'yxati. Sana, mijoz bo'yicha filter.",
                   "document/order", {
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                       "customer_id": {"type": "number", "description": "Mijoz ID"},
                       "status": {"type": "string", "description": "Holat bo'yicha filter"},
                   })

        _get_by_id("ibox_get_order", "Bitta buyurtma tafsilotlari (ID bo'yicha).", "document/order")

        _paginated("ibox_get_sales_by_product", "Tovar bo'yicha sotuv hisoboti.",
                   "report/sale-by-product", {
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                       "category_id": {"type": "number", "description": "Kategoriya bo'yicha filter"},
                       "warehouse_id": {"type": "number", "description": "Ombor bo'yicha filter"},
                   })

        _paginated("ibox_get_shipments", "Yetkazib berish (jo'natish) hisoboti.",
                   "report/shipment", {
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                       "customer_id": {"type": "number", "description": "Mijoz ID"},
                   })

        # ═══════════════════════════════════════
        # XARIDLAR (Purchases) — 2 tools
        # ═══════════════════════════════════════
        _paginated("ibox_get_purchases", "Xaridlar hisoboti. Sana bo'yicha filter.",
                   "report/by-purchase", {
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                       "supplier_id": {"type": "number", "description": "Ta'minotchi ID"},
                   })

        _paginated("ibox_get_purchase_returns", "Qaytarilgan xaridlar hisoboti.",
                   "report/by-purchase-return", {
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                   })

        # ═══════════════════════════════════════
        # TO'LOVLAR (Payments) — 3 tools
        # ═══════════════════════════════════════
        _paginated("ibox_get_payments_received", "Mijozlardan qabul qilingan to'lovlar.",
                   "document/payment-received", {
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                       "customer_id": {"type": "number", "description": "Mijoz ID"},
                   })

        _paginated("ibox_get_payments_made", "Ta'minotchilarga qilingan to'lovlar.",
                   "document/payment-made", {
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                   })

        _paginated("ibox_get_installments", "Nasiya (bo'lib to'lash) ro'yxati.",
                   "document/installment", {
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                   })

        # ═══════════════════════════════════════
        # HISOBOTLAR (Reports) — 5 tools
        # ═══════════════════════════════════════
        async def get_dashboard(p: dict) -> str:
            try:
                params = {
                    "filter_by": p.get("filter_by", "month"),
                    "currency_id": p.get("currency_id", c.currency_id),
                }
                if p.get("from_date"):
                    params["from_date"] = p["from_date"]
                if p.get("to_date"):
                    params["to_date"] = p["to_date"]
                return self._ok(await c.get("dashboard/dashboard", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("ibox_get_dashboard",
                             "Umumiy dashboard — sotuv, xarid, foyda statistikasi.",
                             {"type": "object", "properties": {
                                 "filter_by": {"type": "string",
                                               "description": "Davr: today, week, month, year, custom (default: month)"},
                                 "from_date": {"type": "string", "description": "Boshlanish sanasi (custom uchun)"},
                                 "to_date": {"type": "string", "description": "Tugash sanasi (custom uchun)"},
                                 "currency_id": {"type": "number", "description": "Valyuta ID (default: UZS)"},
                             }}, get_dashboard))

        _paginated("ibox_get_profit_loss", "Foyda va zarar hisoboti.",
                   "report/profit-and-loss", {
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                   })

        _paginated("ibox_get_profitability", "Rentabellik hisoboti (tovar/kategoriya bo'yicha).",
                   "report/profitability", {
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                       "category_id": {"type": "number", "description": "Kategoriya bo'yicha filter"},
                       "warehouse_id": {"type": "number", "description": "Ombor bo'yicha filter"},
                   })

        _paginated("ibox_get_abc_analysis", "ABC tahlil — tovarlarni A/B/C guruhga ajratish.",
                   "report/abc", {
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                   })

        _paginated("ibox_get_days_in_stock", "Omborda necha kun yotganligi hisoboti.",
                   "report/days-in-stock", {
                       "warehouse_id": {"type": "number", "description": "Ombor ID"},
                       "category_id": {"type": "number", "description": "Kategoriya ID"},
                   })

        # ═══════════════════════════════════════
        # MIJOZLAR (Customers/Outlets) — 3 tools
        # ═══════════════════════════════════════
        _paginated("ibox_get_customers", "Mijozlar hisoboti — qarz, to'lov, buyurtma statistikasi.",
                   "report/customer", {
                       "search": {"type": "string", "description": "Mijoz nomi bo'yicha qidirish"},
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                   })

        _paginated("ibox_get_outlets", "Savdo nuqtalari (do'konlar) ro'yxati.",
                   "admin/outlet", {
                       "search": {"type": "string", "description": "Do'kon nomi bo'yicha qidirish"},
                   })

        _paginated("ibox_get_customer_daily", "Mijoz kunlik hisoboti.",
                   "report/counter-party-daily", {
                       "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                       "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                       "customer_id": {"type": "number", "description": "Mijoz ID"},
                   })

        # ═══════════════════════════════════════
        # UMUMIY (Profile) — 1 tool
        # ═══════════════════════════════════════
        async def get_profile(p: dict) -> str:
            try:
                return self._ok(await c.get("user/profile/me"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("ibox_get_profile", "Akkaunt ma'lumotlari — kim sifatida ulangan.",
                             {"type": "object", "properties": {}}, get_profile))

        return tools
