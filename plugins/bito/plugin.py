"""Bito POS/ERP plugin — sotuvlar, tovarlar, mijozlar, ombor, buyurtmalar."""

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


class BitoClient:
    """HTTP client for Bito POS/ERP API."""

    def __init__(self, api_key: str):
        self.base_url = "https://api.systematicdev.uz/integration-api/integration/api/v2"
        self.api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        self._rate_limiter = asyncio.Semaphore(5)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get(self, path: str, params: dict | None = None) -> Any:
        """GET request."""
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: Any = None) -> Any:
        """POST request."""
        return await self._request("POST", path, body=body)

    async def put(self, path: str, body: Any = None) -> Any:
        """PUT request."""
        return await self._request("PUT", path, body=body)

    async def _request(self, method: str, path: str, body: Any = None, params: dict | None = None) -> Any:
        """Make API request."""
        async with self._rate_limiter:
            session = await self._get_session()
            url = f"{self.base_url}{path}"
            headers = {
                "api-key": self.api_key,
                "Content-Type": "application/json",
            }
            clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None and v != ""}
            async with session.request(
                method,
                url,
                json=body if method != "GET" else None,
                params=clean_params if clean_params else None,
                headers=headers,
            ) as resp:
                if resp.content_type == "application/json":
                    data = await resp.json()
                elif resp.status == 204:
                    data = {}
                else:
                    data = {"_raw": await resp.text()}
                if isinstance(data, dict) and data.get("status_code", 200) >= 400:
                    raise RuntimeError(f"API xato: {data.get('message', 'Noma\'lum xato')}")
                return data.get("data", data) if isinstance(data, dict) else data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class QanotPlugin(Plugin):
    """Bito POS/ERP plugin."""

    name = "bito"
    description = "Bito POS/ERP — sotuvlar, tovarlar, mijozlar, ombor, buyurtmalar"
    tools_md = TOOLS_MD
    soul_append = SOUL_APPEND

    def __init__(self):
        self.client: BitoClient | None = None

    async def setup(self, config: dict) -> None:
        api_key = config.get("api_key", "")
        if not api_key:
            logger.warning("[bito] Missing config (api_key)")
            return
        self.client = BitoClient(api_key)
        logger.info("[bito] Client initialized")

    async def teardown(self) -> None:
        if self.client:
            await self.client.close()

    def get_tools(self) -> list[ToolDef]:
        if not self.client:
            return []
        tools = self._build_api_tools()
        logger.info("[bito] %d tools registered", len(tools))
        return tools

    def _ok(self, data: Any) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _err(self, msg: str) -> str:
        return json.dumps({"error": msg})

    def _build_api_tools(self) -> list[ToolDef]:
        c = self.client
        assert c is not None

        tools: list[ToolDef] = []

        def _paging(name: str, desc: str, path: str, params_schema: dict, extra_body_keys: list[str] | None = None):
            """Helper for POST paging endpoints."""
            async def handler(p: dict, _path=path, _extra=extra_body_keys) -> str:
                try:
                    body: dict[str, Any] = {
                        "page": p.get("page", 0),
                        "size": p.get("size", 20),
                    }
                    if _extra:
                        for key in _extra:
                            if key in p and p[key] is not None:
                                body[key] = p[key]
                    return self._ok(await c.post(_path, body))
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc, parameters=params_schema, handler=handler))

        def _get_by_id(name: str, desc: str, path_template: str, id_param: str):
            """Helper for GET by ID endpoints."""
            async def handler(p: dict, _path=path_template, _pk=id_param) -> str:
                try:
                    actual_path = _path.replace(f"{{{_pk}}}", str(p[_pk]))
                    return self._ok(await c.get(actual_path))
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc, parameters={
                "type": "object", "required": [id_param], "properties": {
                    id_param: {"type": "number", "description": f"{id_param.upper().replace('_', ' ')}"},
                }}, handler=handler))

        # ── SOTUVLAR (Trade/Sales) ──
        _paging("bito_get_sales", "Sotuvlar ro'yxati. Sana va tovar bo'yicha filter.", "/trade/get-paging", {
            "type": "object", "properties": {
                "page": {"type": "number", "description": "Sahifa raqami (0 dan boshlanadi)"},
                "size": {"type": "number", "description": "Har sahifadagi natijalar (default 20)"},
                "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                "product_id": {"type": "number", "description": "Tovar ID bo'yicha filter"},
            }}, extra_body_keys=["from_date", "to_date", "product_id"])

        _get_by_id("bito_get_sale", "Bitta sotuv tafsilotlari (ID bo'yicha).", "/trade/get-by-id/{id}", "id")

        # Create sale
        async def create_sale(p: dict) -> str:
            try:
                body: dict[str, Any] = {}
                for key in ("customer_id", "warehouse_id", "cashbox_id", "currency_id",
                            "items", "payment_type", "discount", "note"):
                    if key in p and p[key] is not None:
                        body[key] = p[key]
                return self._ok(await c.post("/trade/create", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bito_create_sale", "Yangi sotuv yaratish. Tovarlar, mijoz, ombor ko'rsatiladi.", {
            "type": "object", "required": ["items"], "properties": {
                "customer_id": {"type": "number", "description": "Mijoz ID"},
                "warehouse_id": {"type": "number", "description": "Ombor ID"},
                "cashbox_id": {"type": "number", "description": "Kassa ID"},
                "currency_id": {"type": "number", "description": "Valyuta ID"},
                "items": {"type": "array", "description": "Tovarlar ro'yxati [{product_id, quantity, price}]",
                          "items": {"type": "object", "properties": {
                              "product_id": {"type": "number"},
                              "quantity": {"type": "number"},
                              "price": {"type": "number"},
                          }}},
                "payment_type": {"type": "string", "description": "To'lov turi (cash, card, transfer)"},
                "discount": {"type": "number", "description": "Chegirma (foiz yoki summa)"},
                "note": {"type": "string", "description": "Izoh"},
            }}, create_sale))

        # ── TOVARLAR (Products) ──
        _paging("bito_get_products", "Tovarlar ro'yxati. Nomi yoki kategoriya bo'yicha qidirish.", "/product/get-paging", {
            "type": "object", "properties": {
                "page": {"type": "number", "description": "Sahifa raqami (0 dan boshlanadi)"},
                "size": {"type": "number", "description": "Har sahifadagi natijalar (default 20)"},
                "search": {"type": "string", "description": "Tovar nomi bo'yicha qidirish"},
                "category_id": {"type": "number", "description": "Kategoriya ID bo'yicha filter"},
            }}, extra_body_keys=["search", "category_id"])

        _get_by_id("bito_get_product", "Bitta tovar tafsilotlari (ID bo'yicha).", "/product/get-by-id/{id}", "id")

        # Create product
        async def create_product(p: dict) -> str:
            try:
                body: dict[str, Any] = {"name": p.get("name", "")}
                for key in ("barcode", "category_id", "price", "cost_price",
                            "unit", "sku", "description"):
                    if key in p and p[key] is not None:
                        body[key] = p[key]
                return self._ok(await c.post("/product/create", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bito_create_product", "Yangi tovar yaratish (nomi, narxi, kategoriya).", {
            "type": "object", "required": ["name"], "properties": {
                "name": {"type": "string", "description": "Tovar nomi"},
                "barcode": {"type": "string", "description": "Shtrix kod"},
                "category_id": {"type": "number", "description": "Kategoriya ID"},
                "price": {"type": "number", "description": "Sotuv narxi"},
                "cost_price": {"type": "number", "description": "Tan narxi"},
                "unit": {"type": "string", "description": "O'lchov birligi (dona, kg, litr)"},
                "sku": {"type": "string", "description": "SKU (artikul)"},
                "description": {"type": "string", "description": "Tovar tavsifi"},
            }}, create_product))

        # ── MIJOZLAR (Customers) ──
        _paging("bito_get_customers", "Mijozlar ro'yxati. Ism yoki telefon bo'yicha qidirish.", "/customer/get-paging", {
            "type": "object", "properties": {
                "page": {"type": "number", "description": "Sahifa raqami (0 dan boshlanadi)"},
                "size": {"type": "number", "description": "Har sahifadagi natijalar (default 20)"},
                "search": {"type": "string", "description": "Mijoz nomi yoki telefon bo'yicha qidirish"},
            }}, extra_body_keys=["search"])

        _get_by_id("bito_get_customer", "Bitta mijoz tafsilotlari (ID bo'yicha).", "/customer/get-by-id/{id}", "id")

        # Create customer
        async def create_customer(p: dict) -> str:
            try:
                body: dict[str, Any] = {"name": p.get("name", "")}
                for key in ("phone", "address", "note", "company"):
                    if key in p and p[key] is not None:
                        body[key] = p[key]
                return self._ok(await c.post("/customer/create", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bito_create_customer", "Yangi mijoz yaratish (ism, telefon, manzil).", {
            "type": "object", "required": ["name"], "properties": {
                "name": {"type": "string", "description": "Mijoz ismi"},
                "phone": {"type": "string", "description": "Telefon raqam (+998...)"},
                "address": {"type": "string", "description": "Manzil"},
                "note": {"type": "string", "description": "Izoh"},
                "company": {"type": "string", "description": "Kompaniya nomi"},
            }}, create_customer))

        # ── OMBOR (Warehouse/Stock) ──
        async def get_warehouses(p: dict) -> str:
            try:
                return self._ok(await c.post("/warehouse/get-all", {}))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bito_get_warehouses", "Barcha omborlar ro'yxati.", {
            "type": "object", "properties": {}}, get_warehouses))

        _paging("bito_get_stock", "Tovarlar qoldiq (zaxira) ma'lumotlari. Ombordagi tovarlar soni.", "/product-stock/get-paging", {
            "type": "object", "properties": {
                "page": {"type": "number", "description": "Sahifa raqami (0 dan boshlanadi)"},
                "size": {"type": "number", "description": "Har sahifadagi natijalar (default 20)"},
                "product_id": {"type": "number", "description": "Tovar ID bo'yicha filter"},
                "warehouse_id": {"type": "number", "description": "Ombor ID bo'yicha filter"},
            }}, extra_body_keys=["product_id", "warehouse_id"])

        # ── XARIDLAR (Purchase/Income) ──
        _paging("bito_get_purchases", "Xaridlar (kirim) ro'yxati. Sana bo'yicha filter.", "/income/get-paging", {
            "type": "object", "properties": {
                "page": {"type": "number", "description": "Sahifa raqami (0 dan boshlanadi)"},
                "size": {"type": "number", "description": "Har sahifadagi natijalar (default 20)"},
                "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
            }}, extra_body_keys=["from_date", "to_date"])

        _get_by_id("bito_get_purchase", "Bitta xarid (kirim) tafsilotlari (ID bo'yicha).", "/income/get-by-id/{id}", "id")

        # ── BUYURTMALAR (Sale Orders) ──
        _paging("bito_get_orders", "Buyurtmalar ro'yxati.", "/sale-order/get-paging", {
            "type": "object", "properties": {
                "page": {"type": "number", "description": "Sahifa raqami (0 dan boshlanadi)"},
                "size": {"type": "number", "description": "Har sahifadagi natijalar (default 20)"},
                "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
                "status": {"type": "string", "description": "Buyurtma holati bo'yicha filter"},
            }}, extra_body_keys=["from_date", "to_date", "status"])

        _get_by_id("bito_get_order", "Bitta buyurtma tafsilotlari (ID bo'yicha).", "/sale-order/get-by-id/{id}", "id")

        # Create order
        async def create_order(p: dict) -> str:
            try:
                body: dict[str, Any] = {}
                for key in ("customer_id", "warehouse_id", "items", "note",
                            "delivery_date", "discount"):
                    if key in p and p[key] is not None:
                        body[key] = p[key]
                return self._ok(await c.post("/sale-order/create", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bito_create_order", "Yangi buyurtma yaratish.", {
            "type": "object", "required": ["items"], "properties": {
                "customer_id": {"type": "number", "description": "Mijoz ID"},
                "warehouse_id": {"type": "number", "description": "Ombor ID"},
                "items": {"type": "array", "description": "Tovarlar ro'yxati [{product_id, quantity, price}]",
                          "items": {"type": "object", "properties": {
                              "product_id": {"type": "number"},
                              "quantity": {"type": "number"},
                              "price": {"type": "number"},
                          }}},
                "note": {"type": "string", "description": "Izoh"},
                "delivery_date": {"type": "string", "description": "Yetkazish sanasi (yyyy-MM-dd)"},
                "discount": {"type": "number", "description": "Chegirma"},
            }}, create_order))

        # ── TA'MINOTCHILAR (Suppliers) ──
        _paging("bito_get_suppliers", "Ta'minotchilar ro'yxati.", "/supplier/get-paging", {
            "type": "object", "properties": {
                "page": {"type": "number", "description": "Sahifa raqami (0 dan boshlanadi)"},
                "size": {"type": "number", "description": "Har sahifadagi natijalar (default 20)"},
                "search": {"type": "string", "description": "Ta'minotchi nomi bo'yicha qidirish"},
            }}, extra_body_keys=["search"])

        # ── HISOBOTLAR (Reports) ──
        async def get_sales_summary(p: dict) -> str:
            try:
                body: dict[str, Any] = {}
                for key in ("from_date", "to_date"):
                    if key in p and p[key] is not None:
                        body[key] = p[key]
                return self._ok(await c.post("/summary/chart", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bito_get_sales_summary", "Sotuv hisoboti — umumiy statistika (grafik uchun ma'lumot).", {
            "type": "object", "properties": {
                "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
            }}, get_sales_summary))

        async def get_sales_by_product(p: dict) -> str:
            try:
                body: dict[str, Any] = {
                    "page": p.get("page", 0),
                    "size": p.get("size", 20),
                }
                for key in ("from_date", "to_date"):
                    if key in p and p[key] is not None:
                        body[key] = p[key]
                return self._ok(await c.post("/sales/by-item-pagin", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bito_get_sales_by_product", "Tovar bo'yicha sotuv hisoboti — qaysi tovar qancha sotilgan.", {
            "type": "object", "properties": {
                "page": {"type": "number", "description": "Sahifa raqami (0 dan boshlanadi)"},
                "size": {"type": "number", "description": "Har sahifadagi natijalar (default 20)"},
                "from_date": {"type": "string", "description": "Boshlanish sanasi (yyyy-MM-dd)"},
                "to_date": {"type": "string", "description": "Tugash sanasi (yyyy-MM-dd)"},
            }}, get_sales_by_product))

        # ── UMUMIY (Profile) ──
        async def get_profile(p: dict) -> str:
            try:
                return self._ok(await c.get("/profile/getMe"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bito_get_profile", "Akkaunt ma'lumotlari — kim sifatida ulangan.", {
            "type": "object", "properties": {}}, get_profile))

        return tools
