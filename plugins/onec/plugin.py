"""1C Enterprise (buxgalteriya) plugin — contractors, products, sales, purchases, balances."""

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


class OneCClient:
    """HTTP client for 1C Enterprise OData REST API."""

    def __init__(self, base_url: str, username: str, password: str):
        self.odata_url = f"{base_url.rstrip('/')}/odata/standard.odata"
        self._auth = aiohttp.BasicAuth(username, password)
        self._session: aiohttp.ClientSession | None = None
        self._rate_limiter = asyncio.Semaphore(3)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get(self, resource: str, params: dict | None = None, top: int = 50) -> Any:
        """GET request to OData endpoint."""
        query: dict[str, str] = {"$format": "json", "$top": str(top)}
        if params:
            query.update(params)
        return await self._request("GET", resource, params=query)

    async def get_all(self, resource: str, params: dict | None = None, page_size: int = 100) -> list[Any]:
        """Fetch all records with pagination."""
        all_records: list[Any] = []
        skip = 0
        while True:
            query: dict[str, str] = {"$format": "json", "$top": str(page_size), "$skip": str(skip)}
            if params:
                query.update(params)
            data = await self._request("GET", resource, params=query)
            records = data.get("value", []) if isinstance(data, dict) else []
            if not records:
                break
            all_records.extend(records)
            if len(records) < page_size:
                break
            skip += page_size
        return all_records

    async def post(self, resource: str, data: dict) -> Any:
        """POST to create new object."""
        return await self._request("POST", resource, body=data)

    async def patch(self, resource_with_key: str, data: dict) -> Any:
        """PATCH to update object."""
        return await self._request("PATCH", resource_with_key, body=data)

    async def _request(
        self,
        method: str,
        resource: str,
        body: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        """Make OData request with rate limiting."""
        async with self._rate_limiter:
            session = await self._get_session()
            url = f"{self.odata_url}/{resource}"
            clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None and v != ""}
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            async with session.request(
                method,
                url,
                json=body if method != "GET" else None,
                params=clean_params,
                headers=headers,
                auth=self._auth,
            ) as resp:
                if resp.content_type and "json" in resp.content_type:
                    data = await resp.json()
                elif resp.status == 204:
                    data = {}
                else:
                    data = {"_raw": await resp.text()}
                if resp.status >= 400:
                    raise RuntimeError(
                        f"1C API xato {resp.status}: {json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)}"[:300]
                    )
                return data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class QanotPlugin(Plugin):
    """1C Enterprise buxgalteriya plugin."""

    name = "onec"
    description = "1C Enterprise — kontragentlar, tovarlar, sotuvlar, xaridlar, kassa"
    tools_md = TOOLS_MD
    soul_append = SOUL_APPEND

    def __init__(self):
        self.client: OneCClient | None = None

    async def setup(self, config: dict) -> None:
        base_url = config.get("base_url", "")
        username = config.get("username", "")
        password = config.get("password", "")
        if not base_url or not username:
            logger.warning("[onec] Missing config (base_url, username)")
            return
        self.client = OneCClient(base_url, username, password)
        logger.info("[onec] Client initialized for %s", base_url)

    async def teardown(self) -> None:
        if self.client:
            await self.client.close()

    def get_tools(self) -> list[ToolDef]:
        if not self.client:
            return []
        tools = self._build_api_tools()
        logger.info("[onec] %d tools registered", len(tools))
        return tools

    def _ok(self, data: Any) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _err(self, msg: str) -> str:
        return json.dumps({"error": msg})

    def _build_api_tools(self) -> list[ToolDef]:
        c = self.client
        assert c is not None

        tools: list[ToolDef] = []

        def _simple(
            name: str,
            desc: str,
            resource: str,
            params_schema: dict,
            key_param: str | None = None,
            top: int = 50,
        ):
            """Register a simple GET tool."""
            async def handler(
                p: dict,
                _res: str = resource,
                _kp: str | None = key_param,
                _top: int = top,
            ) -> str:
                try:
                    actual_res = _res
                    if _kp and _kp in p:
                        key_val = p.pop(_kp)
                        actual_res = f"{_res}(guid'{key_val}')"
                    odata_params: dict[str, str] = {}
                    # Map safe param names to OData params
                    if p.get("filter"):
                        odata_params["$filter"] = p["filter"]
                    if p.get("select"):
                        odata_params["$select"] = p["select"]
                    if p.get("orderby"):
                        odata_params["$orderby"] = p["orderby"]
                    if p.get("top"):
                        _top = int(p["top"])
                    data = await c.get(actual_res, odata_params if odata_params else None, top=_top)
                    return self._ok(data)
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc, parameters=params_schema, handler=handler))

        # ── KONTRAGENTLAR (Contractors/Partners) ──
        async def get_contractors(p: dict) -> str:
            try:
                odata_params: dict[str, str] = {}
                if p.get("search"):
                    odata_params["$filter"] = f"substringof('{p['search']}', Description)"
                top = int(p.get("top", 50))
                data = await c.get("Catalog_Контрагенты", odata_params if odata_params else None, top=top)
                return self._ok(data)
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("onec_get_contractors", "Kontragentlar (hamkorlar) ro'yxati. Nomi bo'yicha qidirish mumkin.", {
            "type": "object", "properties": {
                "search": {"type": "string", "description": "Qidiruv so'zi (kontragent nomi)"},
                "top": {"type": "number", "description": "Natijalar soni (default 50)"},
            }}, get_contractors))

        _simple("onec_get_contractor", "Bitta kontragent tafsilotlari (ID bo'yicha).", "Catalog_Контрагенты", {
            "type": "object", "required": ["ref_key"], "properties": {
                "ref_key": {"type": "string", "description": "Kontragent identifikatori (Ref_Key)"},
            }}, "ref_key")

        # Create contractor
        async def create_contractor(p: dict) -> str:
            try:
                body: dict[str, Any] = {"Description": p.get("name", "")}
                if p.get("inn"):
                    body["ИНН"] = p["inn"]
                if p.get("kpp"):
                    body["КПП"] = p["kpp"]
                if p.get("full_name"):
                    body["НаименованиеПолное"] = p["full_name"]
                if p.get("comment"):
                    body["Комментарий"] = p["comment"]
                return self._ok(await c.post("Catalog_Контрагенты", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("onec_create_contractor", "Yangi kontragent (hamkor) yaratish.", {
            "type": "object", "required": ["name"], "properties": {
                "name": {"type": "string", "description": "Kontragent nomi"},
                "inn": {"type": "string", "description": "INN (soliq raqami)"},
                "kpp": {"type": "string", "description": "KPP"},
                "full_name": {"type": "string", "description": "To'liq nomi"},
                "comment": {"type": "string", "description": "Izoh"},
            }}, create_contractor))

        # ── NOMENKLATURA (Products/Items) ──
        async def get_products(p: dict) -> str:
            try:
                odata_params: dict[str, str] = {}
                filters: list[str] = []
                if p.get("search"):
                    filters.append(f"substringof('{p['search']}', Description)")
                if p.get("group_key"):
                    filters.append(f"Parent_Key eq guid'{p['group_key']}'")
                if filters:
                    odata_params["$filter"] = " and ".join(filters)
                top = int(p.get("top", 50))
                data = await c.get("Catalog_Номенклатура", odata_params if odata_params else None, top=top)
                return self._ok(data)
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("onec_get_products", "Tovarlar (nomenklatura) ro'yxati. Nomi yoki guruh bo'yicha qidirish.", {
            "type": "object", "properties": {
                "search": {"type": "string", "description": "Qidiruv so'zi (tovar nomi)"},
                "group_key": {"type": "string", "description": "Guruh identifikatori (Parent_Key)"},
                "top": {"type": "number", "description": "Natijalar soni (default 50)"},
            }}, get_products))

        _simple("onec_get_product", "Bitta tovar tafsilotlari (ID bo'yicha).", "Catalog_Номенклатура", {
            "type": "object", "required": ["ref_key"], "properties": {
                "ref_key": {"type": "string", "description": "Tovar identifikatori (Ref_Key)"},
            }}, "ref_key")

        # ── SOTUVLAR (Sales Documents) ──
        async def get_sales(p: dict) -> str:
            try:
                odata_params: dict[str, str] = {}
                filters: list[str] = []
                if p.get("date_from"):
                    filters.append(f"Date ge datetime'{p['date_from']}T00:00:00'")
                if p.get("date_to"):
                    filters.append(f"Date le datetime'{p['date_to']}T23:59:59'")
                if p.get("contractor_key"):
                    filters.append(f"Контрагент_Key eq guid'{p['contractor_key']}'")
                if filters:
                    odata_params["$filter"] = " and ".join(filters)
                if p.get("orderby"):
                    odata_params["$orderby"] = p["orderby"]
                top = int(p.get("top", 50))
                data = await c.get("Document_РеализацияТоваровУслуг", odata_params if odata_params else None, top=top)
                return self._ok(data)
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("onec_get_sales", "Sotuvlar hujjatlari ro'yxati. Sana va kontragent bo'yicha filter.", {
            "type": "object", "properties": {
                "date_from": {"type": "string", "description": "Boshlanish sanasi (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "Tugash sanasi (YYYY-MM-DD)"},
                "contractor_key": {"type": "string", "description": "Kontragent identifikatori"},
                "orderby": {"type": "string", "description": "Saralash (masalan: Date desc)"},
                "top": {"type": "number", "description": "Natijalar soni (default 50)"},
            }}, get_sales))

        _simple("onec_get_sale", "Bitta sotuv hujjati tafsilotlari.", "Document_РеализацияТоваровУслуг", {
            "type": "object", "required": ["ref_key"], "properties": {
                "ref_key": {"type": "string", "description": "Hujjat identifikatori (Ref_Key)"},
            }}, "ref_key")

        # Sales summary
        async def get_sales_summary(p: dict) -> str:
            try:
                odata_params: dict[str, str] = {}
                filters: list[str] = []
                if p.get("date_from"):
                    filters.append(f"Date ge datetime'{p['date_from']}T00:00:00'")
                if p.get("date_to"):
                    filters.append(f"Date le datetime'{p['date_to']}T23:59:59'")
                if filters:
                    odata_params["$filter"] = " and ".join(filters)
                all_sales = await c.get_all("Document_РеализацияТоваровУслуг", odata_params if odata_params else None)

                total_sum = sum(doc.get("СуммаДокумента", 0) or 0 for doc in all_sales)
                by_contractor: dict[str, dict[str, Any]] = {}
                for doc in all_sales:
                    ck = doc.get("Контрагент_Key", "noma'lum")
                    desc = doc.get("Контрагент", ck)
                    by_contractor.setdefault(str(desc), {"soni": 0, "summa": 0})
                    by_contractor[str(desc)]["soni"] += 1
                    by_contractor[str(desc)]["summa"] += doc.get("СуммаДокумента", 0) or 0

                return self._ok({
                    "jami_sotuvlar_soni": len(all_sales),
                    "jami_summa": total_sum,
                    "kontragent_boyicha": by_contractor,
                })
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("onec_get_sales_summary",
            "Sotuvlar umumiy hisoboti — BARCHA sotuvlarni o'qib jami hisoblaydi.",
            {"type": "object", "properties": {
                "date_from": {"type": "string", "description": "Boshlanish sanasi (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "Tugash sanasi (YYYY-MM-DD)"},
            }}, get_sales_summary))

        # ── XARIDLAR (Purchase Documents) ──
        async def get_purchases(p: dict) -> str:
            try:
                odata_params: dict[str, str] = {}
                filters: list[str] = []
                if p.get("date_from"):
                    filters.append(f"Date ge datetime'{p['date_from']}T00:00:00'")
                if p.get("date_to"):
                    filters.append(f"Date le datetime'{p['date_to']}T23:59:59'")
                if p.get("contractor_key"):
                    filters.append(f"Контрагент_Key eq guid'{p['contractor_key']}'")
                if filters:
                    odata_params["$filter"] = " and ".join(filters)
                top = int(p.get("top", 50))
                data = await c.get("Document_ПоступлениеТоваровУслуг", odata_params if odata_params else None, top=top)
                return self._ok(data)
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("onec_get_purchases", "Xaridlar hujjatlari ro'yxati. Sana va kontragent bo'yicha filter.", {
            "type": "object", "properties": {
                "date_from": {"type": "string", "description": "Boshlanish sanasi (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "Tugash sanasi (YYYY-MM-DD)"},
                "contractor_key": {"type": "string", "description": "Kontragent identifikatori"},
                "top": {"type": "number", "description": "Natijalar soni (default 50)"},
            }}, get_purchases))

        _simple("onec_get_purchase", "Bitta xarid hujjati tafsilotlari.", "Document_ПоступлениеТоваровУслуг", {
            "type": "object", "required": ["ref_key"], "properties": {
                "ref_key": {"type": "string", "description": "Hujjat identifikatori (Ref_Key)"},
            }}, "ref_key")

        # ── KASSA (Cash Documents) ──
        async def get_cash_receipts(p: dict) -> str:
            try:
                odata_params: dict[str, str] = {}
                filters: list[str] = []
                if p.get("date_from"):
                    filters.append(f"Date ge datetime'{p['date_from']}T00:00:00'")
                if p.get("date_to"):
                    filters.append(f"Date le datetime'{p['date_to']}T23:59:59'")
                if filters:
                    odata_params["$filter"] = " and ".join(filters)
                top = int(p.get("top", 50))
                data = await c.get("Document_ПриходныйКассовыйОрдер", odata_params if odata_params else None, top=top)
                return self._ok(data)
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("onec_get_cash_receipts", "Kassa kirim orderlari. Sana bo'yicha filter.", {
            "type": "object", "properties": {
                "date_from": {"type": "string", "description": "Boshlanish sanasi (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "Tugash sanasi (YYYY-MM-DD)"},
                "top": {"type": "number", "description": "Natijalar soni (default 50)"},
            }}, get_cash_receipts))

        async def get_cash_expenses(p: dict) -> str:
            try:
                odata_params: dict[str, str] = {}
                filters: list[str] = []
                if p.get("date_from"):
                    filters.append(f"Date ge datetime'{p['date_from']}T00:00:00'")
                if p.get("date_to"):
                    filters.append(f"Date le datetime'{p['date_to']}T23:59:59'")
                if filters:
                    odata_params["$filter"] = " and ".join(filters)
                top = int(p.get("top", 50))
                data = await c.get("Document_РасходныйКассовыйОрдер", odata_params if odata_params else None, top=top)
                return self._ok(data)
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("onec_get_cash_expenses", "Kassa chiqim orderlari. Sana bo'yicha filter.", {
            "type": "object", "properties": {
                "date_from": {"type": "string", "description": "Boshlanish sanasi (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "Tugash sanasi (YYYY-MM-DD)"},
                "top": {"type": "number", "description": "Natijalar soni (default 50)"},
            }}, get_cash_expenses))

        # ── QOLDIQLAR (Contractor Balances) ──
        async def get_contractor_balance(p: dict) -> str:
            try:
                odata_params: dict[str, str] = {}
                filters: list[str] = []
                if p.get("contractor_key"):
                    filters.append(f"Контрагент_Key eq guid'{p['contractor_key']}'")
                if filters:
                    odata_params["$filter"] = " and ".join(filters)
                top = int(p.get("top", 100))
                data = await c.get(
                    "AccumulationRegister_ВзаиморасчетыСКонтрагентами",
                    odata_params if odata_params else None,
                    top=top,
                )
                return self._ok(data)
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("onec_get_contractor_balance", "Kontragent bilan o'zaro hisob-kitob qoldig'i.", {
            "type": "object", "properties": {
                "contractor_key": {"type": "string", "description": "Kontragent identifikatori"},
                "top": {"type": "number", "description": "Natijalar soni (default 100)"},
            }}, get_contractor_balance))

        # ── TASHKILOTLAR (Organizations) ──
        _simple("onec_get_organizations", "Tashkilotlar ro'yxati.", "Catalog_Организации", {
            "type": "object", "properties": {
                "top": {"type": "number", "description": "Natijalar soni (default 50)"},
            }})

        # ── OMBORLAR (Warehouses) ──
        _simple("onec_get_warehouses", "Omborlar ro'yxati.", "Catalog_Склады", {
            "type": "object", "properties": {
                "top": {"type": "number", "description": "Natijalar soni (default 50)"},
            }})

        # ── VALYUTA KURSLARI (Exchange Rates) ──
        async def get_exchange_rates(p: dict) -> str:
            try:
                odata_params: dict[str, str] = {}
                filters: list[str] = []
                if p.get("date"):
                    filters.append(f"Period eq datetime'{p['date']}T00:00:00'")
                if p.get("currency_key"):
                    filters.append(f"Валюта_Key eq guid'{p['currency_key']}'")
                if filters:
                    odata_params["$filter"] = " and ".join(filters)
                odata_params["$orderby"] = "Period desc"
                top = int(p.get("top", 50))
                data = await c.get("InformationRegister_КурсыВалют", odata_params, top=top)
                return self._ok(data)
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("onec_get_exchange_rates", "Valyuta kurslari. Sana va valyuta bo'yicha filter.", {
            "type": "object", "properties": {
                "date": {"type": "string", "description": "Sana (YYYY-MM-DD)"},
                "currency_key": {"type": "string", "description": "Valyuta identifikatori"},
                "top": {"type": "number", "description": "Natijalar soni (default 50)"},
            }}, get_exchange_rates))

        # ── METADATA ──
        async def get_metadata(p: dict) -> str:
            try:
                session = await c._get_session()
                url = f"{c.odata_url}/$metadata"
                async with session.get(url, auth=c._auth) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        return self._err(f"1C metadata xato {resp.status}")
                # Extract entity names from XML
                import re
                entities = re.findall(r'EntityType\s+Name="([^"]+)"', text)
                entity_sets = re.findall(r'EntitySet\s+Name="([^"]+)"', text)
                return self._ok({
                    "entity_types": entities[:100],
                    "entity_sets": entity_sets[:100],
                    "jami": len(entity_sets),
                })
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("onec_get_metadata", "1C bazasidagi barcha mavjud ob'ektlar ro'yxati (metadata).", {
            "type": "object", "properties": {}}, get_metadata))

        # ── UMUMIY QUERY (Custom OData Query) ──
        async def custom_query(p: dict) -> str:
            try:
                resource = p.get("resource", "")
                if not resource:
                    return self._err("resource majburiy parametr")
                odata_params: dict[str, str] = {}
                if p.get("filter"):
                    odata_params["$filter"] = p["filter"]
                if p.get("select"):
                    odata_params["$select"] = p["select"]
                if p.get("orderby"):
                    odata_params["$orderby"] = p["orderby"]
                top = int(p.get("top", 50))
                data = await c.get(resource, odata_params if odata_params else None, top=top)
                return self._ok(data)
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("onec_query", "Ixtiyoriy 1C ob'ektga so'rov — har qanday resurs va filter bilan.", {
            "type": "object", "required": ["resource"], "properties": {
                "resource": {"type": "string", "description": "OData resurs nomi (masalan: Catalog_Контрагенты, Document_СчетНаОплату)"},
                "filter": {"type": "string", "description": "OData filter ifodasi"},
                "select": {"type": "string", "description": "Tanlangan maydonlar (vergul bilan)"},
                "orderby": {"type": "string", "description": "Saralash (masalan: Date desc)"},
                "top": {"type": "number", "description": "Natijalar soni (default 50)"},
            }}, custom_query))

        return tools
