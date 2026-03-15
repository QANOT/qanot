"""Bitrix24 CRM plugin — deals, leads, contacts, tasks, activities."""

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


class Bitrix24Client:
    """HTTP client for Bitrix24 REST API."""

    def __init__(self, domain: str, user_id: str, webhook_code: str):
        self.base_url = f"https://{domain}/rest/{user_id}/{webhook_code}"
        self._session: aiohttp.ClientSession | None = None
        self._rate_limiter = asyncio.Semaphore(2)  # 2 req/s limit

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def call(self, method: str, params: dict | None = None) -> Any:
        """Call a Bitrix24 API method. All methods use POST."""
        async with self._rate_limiter:
            session = await self._get_session()
            url = f"{self.base_url}/{method}"
            async with session.post(url, json=params or {}) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"{data['error']}: {data.get('error_description', '')}")
                return data

    async def call_all(self, method: str, params: dict | None = None) -> list:
        """Fetch all pages of a paginated method."""
        all_items: list = []
        start = 0
        while True:
            p = {**(params or {}), "start": start}
            data = await self.call(method, p)
            items = data.get("result", [])
            if isinstance(items, dict):
                # Some methods return dict with nested list
                items = list(items.values())[0] if items else []
            all_items.extend(items)
            if "next" not in data:
                break
            start = data["next"]
        return all_items

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class QanotPlugin(Plugin):
    """Bitrix24 CRM plugin."""

    name = "bitrix24"
    description = "Bitrix24 — sdelkalar, lidlar, kontaktlar, vazifalar, kompaniyalar"
    tools_md = TOOLS_MD
    soul_append = SOUL_APPEND

    def __init__(self):
        self.client: Bitrix24Client | None = None

    async def setup(self, config: dict) -> None:
        domain = config.get("domain", "")
        user_id = config.get("user_id", "")
        webhook_code = config.get("webhook_code", "")
        if not domain or not user_id or not webhook_code:
            logger.warning("[bitrix24] Missing config (domain, user_id, webhook_code)")
            return
        self.client = Bitrix24Client(domain, user_id, webhook_code)
        logger.info("[bitrix24] Client initialized for %s", domain)

    async def teardown(self) -> None:
        if self.client:
            await self.client.close()

    def get_tools(self) -> list[ToolDef]:
        if not self.client:
            return []
        tools = self._build_api_tools()
        logger.info("[bitrix24] %d tools registered", len(tools))
        return tools

    def _ok(self, data: Any) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _err(self, msg: str) -> str:
        return json.dumps({"error": msg})

    def _build_api_tools(self) -> list[ToolDef]:
        c = self.client
        assert c is not None

        tools: list[ToolDef] = []

        # Map clean param names to Bitrix24 filter keys (for >=, <= prefixes)
        FILTER_KEY_MAP: dict[str, str] = {
            "DATE_CREATE_FROM": ">=DATE_CREATE",
            "DATE_CREATE_TO": "<=DATE_CREATE",
        }

        def _simple(name: str, desc: str, method: str, params_schema: dict,
                    select: list[str] | None = None):
            """Register a simple list/get tool that calls a Bitrix24 method."""
            async def handler(p: dict, _method=method, _select=select) -> str:
                try:
                    params: dict[str, Any] = {}
                    # Build filter from user params
                    filt: dict[str, Any] = {}
                    for k, v in p.items():
                        if v is not None and v != "":
                            filt[FILTER_KEY_MAP.get(k, k)] = v
                    if filt:
                        params["filter"] = filt
                    if _select:
                        params["select"] = _select
                    return self._ok(await c.call(_method, params))
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc, parameters=params_schema, handler=handler))

        def _get_by_id(name: str, desc: str, method: str, id_field: str, params_schema: dict):
            """Register a get-by-ID tool."""
            async def handler(p: dict, _method=method, _id=id_field) -> str:
                try:
                    return self._ok(await c.call(_method, {"ID": p[_id]}))
                except Exception as e:
                    return self._err(str(e))
            tools.append(ToolDef(name=name, description=desc, parameters=params_schema, handler=handler))

        # ── DEALS (Sdelkalar) ──
        _simple("bitrix24_get_deals", "Sdelkalar ro'yxati. Bosqich, kategoriya, mas'ul shaxs bo'yicha filter.", "crm.deal.list", {
            "type": "object", "properties": {
                "STAGE_ID": {"type": "string", "description": "Bosqich ID (masalan NEW, WON, LOSE)"},
                "CATEGORY_ID": {"type": "number", "description": "Voronka (kategoriya) ID"},
                "ASSIGNED_BY_ID": {"type": "number", "description": "Mas'ul shaxs ID"},
                "DATE_CREATE_FROM": {"type": "string", "description": "Yaratilgan sanadan boshlab (YYYY-MM-DD)"},
                "DATE_CREATE_TO": {"type": "string", "description": "Yaratilgan sanagacha (YYYY-MM-DD)"},
            }}, select=["ID", "TITLE", "OPPORTUNITY", "CURRENCY_ID", "STAGE_ID", "CATEGORY_ID", "ASSIGNED_BY_ID", "DATE_CREATE"])

        _get_by_id("bitrix24_get_deal", "Bitta sdelka tafsilotlari.", "crm.deal.get", "deal_id", {
            "type": "object", "required": ["deal_id"], "properties": {
                "deal_id": {"type": "number", "description": "Sdelka ID"},
            }})

        # Create deal
        async def create_deal(p: dict) -> str:
            try:
                fields: dict[str, Any] = {"TITLE": p.get("TITLE", "Yangi sdelka")}
                for key in ("OPPORTUNITY", "CURRENCY_ID", "STAGE_ID", "CATEGORY_ID",
                            "CONTACT_ID", "COMPANY_ID", "ASSIGNED_BY_ID"):
                    if p.get(key) is not None:
                        fields[key] = p[key]
                return self._ok(await c.call("crm.deal.add", {"fields": fields}))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bitrix24_create_deal", "Yangi sdelka yaratish.", {
            "type": "object", "required": ["TITLE"], "properties": {
                "TITLE": {"type": "string", "description": "Sdelka nomi"},
                "OPPORTUNITY": {"type": "number", "description": "Summa"},
                "CURRENCY_ID": {"type": "string", "description": "Valyuta (UZS, USD)"},
                "STAGE_ID": {"type": "string", "description": "Bosqich ID"},
                "CATEGORY_ID": {"type": "number", "description": "Voronka ID"},
                "CONTACT_ID": {"type": "number", "description": "Kontakt ID"},
                "COMPANY_ID": {"type": "number", "description": "Kompaniya ID"},
                "ASSIGNED_BY_ID": {"type": "number", "description": "Mas'ul shaxs ID"},
            }}, create_deal))

        # Update deal
        async def update_deal(p: dict) -> str:
            try:
                deal_id = p.pop("deal_id")
                fields: dict[str, Any] = {}
                for key in ("TITLE", "OPPORTUNITY", "CURRENCY_ID", "STAGE_ID",
                            "CATEGORY_ID", "CONTACT_ID", "COMPANY_ID", "ASSIGNED_BY_ID"):
                    if p.get(key) is not None:
                        fields[key] = p[key]
                return self._ok(await c.call("crm.deal.update", {"ID": deal_id, "fields": fields}))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bitrix24_update_deal", "Sdelka yangilash (bosqich, summa, mas'ul shaxs).", {
            "type": "object", "required": ["deal_id"], "properties": {
                "deal_id": {"type": "number", "description": "Sdelka ID"},
                "TITLE": {"type": "string", "description": "Yangi nom"},
                "OPPORTUNITY": {"type": "number", "description": "Yangi summa"},
                "CURRENCY_ID": {"type": "string", "description": "Valyuta"},
                "STAGE_ID": {"type": "string", "description": "Bosqich ID"},
                "CATEGORY_ID": {"type": "number", "description": "Voronka ID"},
                "CONTACT_ID": {"type": "number", "description": "Kontakt ID"},
                "COMPANY_ID": {"type": "number", "description": "Kompaniya ID"},
                "ASSIGNED_BY_ID": {"type": "number", "description": "Mas'ul shaxs ID"},
            }}, update_deal))

        # Deals summary
        async def deals_summary(p: dict) -> str:
            try:
                params: dict[str, Any] = {"select": ["ID", "TITLE", "OPPORTUNITY", "STAGE_ID", "CATEGORY_ID"]}
                if p.get("CATEGORY_ID") is not None:
                    params["filter"] = {"CATEGORY_ID": p["CATEGORY_ID"]}
                all_deals = await c.call_all("crm.deal.list", params)

                total_sum = sum(float(d.get("OPPORTUNITY", 0) or 0) for d in all_deals)
                by_stage: dict[str, dict[str, Any]] = {}
                for deal in all_deals:
                    stage = deal.get("STAGE_ID", "UNKNOWN")
                    by_stage.setdefault(stage, {"count": 0, "total": 0.0})
                    by_stage[stage]["count"] += 1
                    by_stage[stage]["total"] += float(deal.get("OPPORTUNITY", 0) or 0)

                return self._ok({
                    "jami_sdelkalar_soni": len(all_deals),
                    "jami_summa": total_sum,
                    "bosqich_boyicha": by_stage,
                })
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bitrix24_get_deals_summary",
            "Sdelkalar umumiy hisoboti — BARCHA sahifalarni o'qib jami hisoblaydi.",
            {"type": "object", "properties": {
                "CATEGORY_ID": {"type": "number", "description": "Voronka ID (ixtiyoriy)"},
            }}, deals_summary))

        # ── LEADS (Lidlar) ──
        _simple("bitrix24_get_leads", "Lidlar ro'yxati. Status, manba, mas'ul shaxs bo'yicha filter.", "crm.lead.list", {
            "type": "object", "properties": {
                "STATUS_ID": {"type": "string", "description": "Status ID (NEW, IN_PROCESS, CONVERTED)"},
                "SOURCE_ID": {"type": "string", "description": "Manba ID (WEB, PHONE, EMAIL)"},
                "ASSIGNED_BY_ID": {"type": "number", "description": "Mas'ul shaxs ID"},
            }}, select=["ID", "TITLE", "NAME", "STATUS_ID", "SOURCE_ID", "OPPORTUNITY", "ASSIGNED_BY_ID", "DATE_CREATE"])

        _get_by_id("bitrix24_get_lead", "Bitta lid tafsilotlari.", "crm.lead.get", "lead_id", {
            "type": "object", "required": ["lead_id"], "properties": {
                "lead_id": {"type": "number", "description": "Lid ID"},
            }})

        # Create lead
        async def create_lead(p: dict) -> str:
            try:
                fields: dict[str, Any] = {"TITLE": p.get("TITLE", "Yangi lid")}
                for key in ("NAME", "SOURCE_ID", "STATUS_ID", "ASSIGNED_BY_ID", "OPPORTUNITY"):
                    if p.get(key) is not None:
                        fields[key] = p[key]
                if p.get("PHONE"):
                    fields["PHONE"] = [{"VALUE": p["PHONE"], "VALUE_TYPE": "WORK"}]
                return self._ok(await c.call("crm.lead.add", {"fields": fields}))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bitrix24_create_lead", "Yangi lid yaratish.", {
            "type": "object", "required": ["TITLE"], "properties": {
                "TITLE": {"type": "string", "description": "Lid nomi"},
                "NAME": {"type": "string", "description": "Kontakt ismi"},
                "PHONE": {"type": "string", "description": "Telefon raqam (+998...)"},
                "SOURCE_ID": {"type": "string", "description": "Manba (WEB, PHONE, EMAIL)"},
                "STATUS_ID": {"type": "string", "description": "Status ID"},
                "ASSIGNED_BY_ID": {"type": "number", "description": "Mas'ul shaxs ID"},
                "OPPORTUNITY": {"type": "number", "description": "Summa"},
            }}, create_lead))

        # Update lead
        async def update_lead(p: dict) -> str:
            try:
                lead_id = p.pop("lead_id")
                fields: dict[str, Any] = {}
                for key in ("TITLE", "NAME", "STATUS_ID", "SOURCE_ID", "ASSIGNED_BY_ID", "OPPORTUNITY"):
                    if p.get(key) is not None:
                        fields[key] = p[key]
                if p.get("PHONE"):
                    fields["PHONE"] = [{"VALUE": p["PHONE"], "VALUE_TYPE": "WORK"}]
                return self._ok(await c.call("crm.lead.update", {"ID": lead_id, "fields": fields}))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bitrix24_update_lead", "Lid yangilash (status, summa, mas'ul shaxs).", {
            "type": "object", "required": ["lead_id"], "properties": {
                "lead_id": {"type": "number", "description": "Lid ID"},
                "TITLE": {"type": "string", "description": "Yangi nom"},
                "NAME": {"type": "string", "description": "Kontakt ismi"},
                "PHONE": {"type": "string", "description": "Telefon raqam (+998...)"},
                "STATUS_ID": {"type": "string", "description": "Status ID"},
                "SOURCE_ID": {"type": "string", "description": "Manba ID"},
                "ASSIGNED_BY_ID": {"type": "number", "description": "Mas'ul shaxs ID"},
                "OPPORTUNITY": {"type": "number", "description": "Summa"},
            }}, update_lead))

        # ── CONTACTS (Kontaktlar) ──
        _simple("bitrix24_get_contacts", "Kontaktlar ro'yxati yoki qidirish.", "crm.contact.list", {
            "type": "object", "properties": {
                "NAME": {"type": "string", "description": "Ism bo'yicha qidirish"},
                "LAST_NAME": {"type": "string", "description": "Familiya bo'yicha"},
                "ASSIGNED_BY_ID": {"type": "number", "description": "Mas'ul shaxs ID"},
            }}, select=["ID", "NAME", "LAST_NAME", "PHONE", "EMAIL", "ASSIGNED_BY_ID"])

        _get_by_id("bitrix24_get_contact", "Bitta kontakt ma'lumotlari.", "crm.contact.get", "contact_id", {
            "type": "object", "required": ["contact_id"], "properties": {
                "contact_id": {"type": "number", "description": "Kontakt ID"},
            }})

        # Create contact
        async def create_contact(p: dict) -> str:
            try:
                fields: dict[str, Any] = {"NAME": p.get("NAME", "")}
                if p.get("LAST_NAME"):
                    fields["LAST_NAME"] = p["LAST_NAME"]
                if p.get("PHONE"):
                    fields["PHONE"] = [{"VALUE": p["PHONE"], "VALUE_TYPE": "WORK"}]
                if p.get("EMAIL"):
                    fields["EMAIL"] = [{"VALUE": p["EMAIL"], "VALUE_TYPE": "WORK"}]
                if p.get("ASSIGNED_BY_ID") is not None:
                    fields["ASSIGNED_BY_ID"] = p["ASSIGNED_BY_ID"]
                return self._ok(await c.call("crm.contact.add", {"fields": fields}))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bitrix24_create_contact", "Yangi kontakt yaratish.", {
            "type": "object", "required": ["NAME"], "properties": {
                "NAME": {"type": "string", "description": "Kontakt ismi"},
                "LAST_NAME": {"type": "string", "description": "Familiya"},
                "PHONE": {"type": "string", "description": "Telefon raqam (+998...)"},
                "EMAIL": {"type": "string", "description": "Email manzil"},
                "ASSIGNED_BY_ID": {"type": "number", "description": "Mas'ul shaxs ID"},
            }}, create_contact))

        # ── COMPANIES (Kompaniyalar) ──
        _simple("bitrix24_get_companies", "Kompaniyalar ro'yxati.", "crm.company.list", {
            "type": "object", "properties": {
                "TITLE": {"type": "string", "description": "Kompaniya nomi bo'yicha qidirish"},
                "ASSIGNED_BY_ID": {"type": "number", "description": "Mas'ul shaxs ID"},
            }}, select=["ID", "TITLE", "PHONE", "EMAIL", "ASSIGNED_BY_ID"])

        _get_by_id("bitrix24_get_company", "Bitta kompaniya ma'lumotlari.", "crm.company.get", "company_id", {
            "type": "object", "required": ["company_id"], "properties": {
                "company_id": {"type": "number", "description": "Kompaniya ID"},
            }})

        # ── TASKS (Vazifalar) ──
        async def get_tasks(p: dict) -> str:
            try:
                params: dict[str, Any] = {"select": ["ID", "TITLE", "DESCRIPTION", "DEADLINE", "RESPONSIBLE_ID", "STATUS"]}
                filt: dict[str, Any] = {}
                if p.get("RESPONSIBLE_ID") is not None:
                    filt["RESPONSIBLE_ID"] = p["RESPONSIBLE_ID"]
                if p.get("STATUS") is not None:
                    filt["STATUS"] = p["STATUS"]
                if filt:
                    params["filter"] = filt
                return self._ok(await c.call("tasks.task.list", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bitrix24_get_tasks", "Vazifalar ro'yxati.", {
            "type": "object", "properties": {
                "RESPONSIBLE_ID": {"type": "number", "description": "Mas'ul shaxs ID"},
                "STATUS": {"type": "number", "description": "Holat (2=kutilmoqda, 3=bajarilmoqda, 5=bajarilgan)"},
            }}, get_tasks))

        # Create task
        async def create_task(p: dict) -> str:
            try:
                fields: dict[str, Any] = {"TITLE": p.get("TITLE", "Yangi vazifa")}
                for key in ("DESCRIPTION", "DEADLINE", "RESPONSIBLE_ID"):
                    if p.get(key) is not None:
                        fields[key] = p[key]
                return self._ok(await c.call("tasks.task.add", {"fields": fields}))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bitrix24_create_task", "Yangi vazifa yaratish.", {
            "type": "object", "required": ["TITLE"], "properties": {
                "TITLE": {"type": "string", "description": "Vazifa nomi"},
                "DESCRIPTION": {"type": "string", "description": "Vazifa tavsifi"},
                "DEADLINE": {"type": "string", "description": "Muddat (YYYY-MM-DD HH:MM:SS)"},
                "RESPONSIBLE_ID": {"type": "number", "description": "Mas'ul shaxs ID"},
            }}, create_task))

        # ── ACTIVITIES (Deyatelnosti / Dela) ──
        async def get_activities(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                filt: dict[str, Any] = {}
                if p.get("OWNER_TYPE_ID") is not None:
                    filt["OWNER_TYPE_ID"] = p["OWNER_TYPE_ID"]
                if p.get("OWNER_ID") is not None:
                    filt["OWNER_ID"] = p["OWNER_ID"]
                if p.get("COMPLETED") is not None:
                    filt["COMPLETED"] = p["COMPLETED"]
                if filt:
                    params["filter"] = filt
                params["select"] = ["ID", "SUBJECT", "DESCRIPTION", "TYPE_ID", "OWNER_TYPE_ID", "OWNER_ID", "COMPLETED", "DEADLINE"]
                return self._ok(await c.call("crm.activity.list", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bitrix24_get_activities", "CRM faoliyatlar (dela) ro'yxati.", {
            "type": "object", "properties": {
                "OWNER_TYPE_ID": {"type": "number", "description": "Egalik turi (1=lid, 2=sdelka, 3=kontakt)"},
                "OWNER_ID": {"type": "number", "description": "Egasi ID"},
                "COMPLETED": {"type": "string", "description": "Bajarilganmi (Y yoki N)"},
            }}, get_activities))

        # Create activity
        async def create_activity(p: dict) -> str:
            try:
                fields: dict[str, Any] = {
                    "SUBJECT": p.get("SUBJECT", ""),
                    "OWNER_TYPE_ID": p.get("OWNER_TYPE_ID", 2),
                    "OWNER_ID": p.get("OWNER_ID", 0),
                    "TYPE_ID": p.get("TYPE_ID", 2),
                }
                if p.get("DESCRIPTION"):
                    fields["DESCRIPTION"] = p["DESCRIPTION"]
                if p.get("DEADLINE"):
                    fields["DEADLINE"] = p["DEADLINE"]
                if p.get("RESPONSIBLE_ID") is not None:
                    fields["RESPONSIBLE_ID"] = p["RESPONSIBLE_ID"]
                return self._ok(await c.call("crm.activity.add", {"fields": fields}))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bitrix24_create_activity", "Yangi CRM faoliyat (delo) yaratish.", {
            "type": "object", "required": ["SUBJECT", "OWNER_TYPE_ID", "OWNER_ID"], "properties": {
                "SUBJECT": {"type": "string", "description": "Mavzu"},
                "DESCRIPTION": {"type": "string", "description": "Tavsif"},
                "TYPE_ID": {"type": "number", "description": "Turi (1=uchrashuv, 2=qo'ng'iroq, 3=xat)"},
                "OWNER_TYPE_ID": {"type": "number", "description": "Egalik turi (1=lid, 2=sdelka, 3=kontakt)"},
                "OWNER_ID": {"type": "number", "description": "Egasi ID"},
                "DEADLINE": {"type": "string", "description": "Muddat (YYYY-MM-DD HH:MM:SS)"},
                "RESPONSIBLE_ID": {"type": "number", "description": "Mas'ul shaxs ID"},
            }}, create_activity))

        # ── DEAL STAGES (Bosqichlar) ──
        async def get_deal_stages(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if p.get("CATEGORY_ID") is not None:
                    params["id"] = p["CATEGORY_ID"]
                return self._ok(await c.call("crm.dealcategory.stage.list", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bitrix24_get_deal_stages", "Sdelka bosqichlari (voronka statuslari) ro'yxati.", {
            "type": "object", "properties": {
                "CATEGORY_ID": {"type": "number", "description": "Voronka (kategoriya) ID. Bo'sh = standart voronka."},
            }}, get_deal_stages))

        # ── USERS (Foydalanuvchilar) ──
        async def get_users(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if p.get("ACTIVE") is not None:
                    params["ACTIVE"] = p["ACTIVE"]
                return self._ok(await c.call("user.get", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("bitrix24_get_users", "CRM foydalanuvchilari (menejerlar) ro'yxati.", {
            "type": "object", "properties": {
                "ACTIVE": {"type": "boolean", "description": "Faqat faol foydalanuvchilar (true/false)"},
            }}, get_users))

        return tools
