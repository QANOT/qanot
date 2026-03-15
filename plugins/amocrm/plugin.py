"""amoCRM CRM plugin — leads, contacts, pipelines, tasks, notes."""

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


class AmoCRMClient:
    """HTTP client for amoCRM API v4."""

    def __init__(
        self,
        subdomain: str,
        client_id: str,
        client_secret: str,
        access_token: str,
        refresh_token: str,
        redirect_uri: str,
    ):
        self.base_url = f"https://{subdomain}.amocrm.ru"
        self.api_url = f"{self.base_url}/api/v4"
        self.subdomain = subdomain
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.redirect_uri = redirect_uri
        self._session: aiohttp.ClientSession | None = None
        self._rate_limiter = asyncio.Semaphore(5)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _refresh_tokens(self) -> None:
        """Refresh OAuth tokens when access_token expires."""
        session = await self._get_session()
        async with session.post(f"{self.base_url}/oauth2/access_token", json={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "redirect_uri": self.redirect_uri,
        }) as resp:
            data = await resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"Token refresh failed: {json.dumps(data)[:200]}")
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self._save_tokens()

    async def get(self, path: str, params: dict | None = None) -> Any:
        """GET request."""
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: Any = None) -> Any:
        """POST request."""
        return await self._request("POST", path, body=body)

    async def patch(self, path: str, body: Any = None) -> Any:
        """PATCH request."""
        return await self._request("PATCH", path, body=body)

    async def _request(self, method: str, path: str, body: Any = None, params: dict | None = None) -> Any:
        """Make API request with auto-retry on 401."""
        async with self._rate_limiter:
            data, status = await self._raw(method, path, body, params)
            if status == 401:
                await self._refresh_tokens()
                data, status = await self._raw(method, path, body, params)
            if status >= 400:
                raise RuntimeError(f"API error {status}: {json.dumps(data) if isinstance(data, dict) else str(data)}"[:300])
            return data

    async def _raw(self, method: str, path: str, body: Any = None, params: dict | None = None) -> tuple[Any, int]:
        session = await self._get_session()
        url = f"{self.api_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None and v != ""}
        async with session.request(
            method,
            url,
            json=body if method != "GET" else None,
            params=clean_params,
            headers=headers,
        ) as resp:
            if resp.content_type == "application/json":
                data = await resp.json()
            elif resp.status == 204:
                data = {}
            else:
                data = {"_raw": await resp.text()}
            return data, resp.status

    def _save_tokens(self) -> None:
        """Persist refreshed tokens to config file."""
        token_path = Path("/data/workspace/.amocrm_tokens.json")
        try:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(json.dumps({
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
            }), encoding="utf-8")
        except Exception:
            pass  # best effort

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class QanotPlugin(Plugin):
    """amoCRM CRM plugin."""

    name = "amocrm"
    description = "amoCRM — lidlar, kontaktlar, voronkalar, vazifalar"
    tools_md = TOOLS_MD
    soul_append = SOUL_APPEND

    def __init__(self):
        self.client: AmoCRMClient | None = None

    async def setup(self, config: dict) -> None:
        subdomain = config.get("subdomain", "")
        client_id = config.get("client_id", "")
        client_secret = config.get("client_secret", "")
        access_token = config.get("access_token", "")
        refresh_token = config.get("refresh_token", "")
        redirect_uri = config.get("redirect_uri", "")
        if not subdomain or not access_token or not refresh_token:
            logger.warning("[amocrm] Missing config (subdomain, access_token, refresh_token)")
            return
        # Try loading persisted tokens
        token_path = Path("/data/workspace/.amocrm_tokens.json")
        if token_path.exists():
            try:
                saved = json.loads(token_path.read_text(encoding="utf-8"))
                access_token = saved.get("access_token", access_token)
                refresh_token = saved.get("refresh_token", refresh_token)
            except Exception:
                pass
        self.client = AmoCRMClient(subdomain, client_id, client_secret, access_token, refresh_token, redirect_uri)
        logger.info("[amocrm] Client initialized for %s.amocrm.ru", subdomain)

    async def teardown(self) -> None:
        if self.client:
            await self.client.close()

    def get_tools(self) -> list[ToolDef]:
        if not self.client:
            return []
        tools = self._build_api_tools()
        logger.info("[amocrm] %d tools registered", len(tools))
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

        # ── LEADS (Lidlar) ──
        _simple("amocrm_get_leads", "Lidlar ro'yxati. query, pipeline_id, status bo'yicha filter.", "/leads", {
            "type": "object", "properties": {
                "query": {"type": "string", "description": "Qidiruv so'zi (lid nomi, kontakt)"},
                "page": {"type": "number", "description": "Sahifa raqami"},
                "limit": {"type": "number", "description": "Har sahifadagi natijalar (max 250)"},
                "with": {"type": "string", "description": "Qo'shimcha ma'lumot: contacts,catalog_elements,loss_reason"},
            }})
        _simple("amocrm_get_lead", "Bitta lid tafsilotlari.", "/leads/{lead_id}", {
            "type": "object", "required": ["lead_id"], "properties": {
                "lead_id": {"type": "number", "description": "Lid ID"},
                "with": {"type": "string", "description": "Qo'shimcha: contacts,catalog_elements,loss_reason"},
            }}, "lead_id")

        # Create lead
        async def create_lead(p: dict) -> str:
            try:
                body = [{"name": p.get("name", "Yangi lid")}]
                if p.get("price") is not None:
                    body[0]["price"] = p["price"]
                if p.get("pipeline_id") is not None:
                    body[0]["pipeline_id"] = p["pipeline_id"]
                if p.get("status_id") is not None:
                    body[0]["status_id"] = p["status_id"]
                if p.get("responsible_user_id") is not None:
                    body[0]["responsible_user_id"] = p["responsible_user_id"]
                if p.get("custom_fields_values"):
                    body[0]["custom_fields_values"] = p["custom_fields_values"]
                return self._ok(await c.post("/leads", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("amocrm_create_lead", "Yangi lid yaratish.", {
            "type": "object", "required": ["name"], "properties": {
                "name": {"type": "string", "description": "Lid nomi"},
                "price": {"type": "number", "description": "Narxi (so'm)"},
                "pipeline_id": {"type": "number", "description": "Voronka ID"},
                "status_id": {"type": "number", "description": "Bosqich (status) ID"},
                "responsible_user_id": {"type": "number", "description": "Mas'ul shaxs ID"},
                "custom_fields_values": {"type": "array", "description": "Custom fieldlar [{field_id, values: [{value}]}]"},
            }}, create_lead))

        # Update lead
        async def update_lead(p: dict) -> str:
            try:
                lead_id = p.pop("lead_id")
                body = [{"id": lead_id}]
                for key in ("name", "price", "pipeline_id", "status_id", "responsible_user_id"):
                    if p.get(key) is not None:
                        body[0][key] = p[key]
                if p.get("custom_fields_values"):
                    body[0]["custom_fields_values"] = p["custom_fields_values"]
                return self._ok(await c.patch("/leads", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("amocrm_update_lead", "Lid yangilash (status, narx, custom fieldlar).", {
            "type": "object", "required": ["lead_id"], "properties": {
                "lead_id": {"type": "number", "description": "Lid ID"},
                "name": {"type": "string", "description": "Yangi nom"},
                "price": {"type": "number", "description": "Yangi narx"},
                "pipeline_id": {"type": "number", "description": "Voronka ID"},
                "status_id": {"type": "number", "description": "Bosqich ID"},
                "responsible_user_id": {"type": "number", "description": "Mas'ul shaxs ID"},
                "custom_fields_values": {"type": "array", "description": "Custom fieldlar"},
            }}, update_lead))

        # Leads summary
        async def leads_summary(p: dict) -> str:
            try:
                all_leads: list = []
                page = 1
                while True:
                    params = {**(p or {}), "page": page, "limit": 250}
                    data = await c.get("/leads", params)
                    embedded = data.get("_embedded", {}) if isinstance(data, dict) else {}
                    leads = embedded.get("leads", [])
                    if not leads:
                        break
                    all_leads.extend(leads)
                    if len(leads) < 250:
                        break
                    page += 1

                total_price = sum(lead.get("price", 0) or 0 for lead in all_leads)
                by_pipeline: dict = {}
                by_status: dict = {}
                for lead in all_leads:
                    pid = lead.get("pipeline_id", 0)
                    sid = lead.get("status_id", 0)
                    by_pipeline.setdefault(pid, {"count": 0, "total": 0})
                    by_pipeline[pid]["count"] += 1
                    by_pipeline[pid]["total"] += lead.get("price", 0) or 0
                    by_status.setdefault(sid, {"count": 0, "total": 0})
                    by_status[sid]["count"] += 1
                    by_status[sid]["total"] += lead.get("price", 0) or 0

                return self._ok({
                    "jami_lidlar_soni": len(all_leads),
                    "jami_summa": total_price,
                    "voronka_boyicha": by_pipeline,
                    "status_boyicha": by_status,
                })
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("amocrm_get_leads_summary",
            "Lidlar umumiy hisoboti — BARCHA sahifalarni o'qib jami hisoblaydi.",
            {"type": "object", "properties": {
                "query": {"type": "string", "description": "Qidiruv so'zi"},
            }}, leads_summary))

        # ── CONTACTS (Kontaktlar) ──
        _simple("amocrm_get_contacts", "Kontaktlar ro'yxati yoki qidirish.", "/contacts", {
            "type": "object", "properties": {
                "query": {"type": "string", "description": "Qidiruv so'zi (ism, telefon, email)"},
                "page": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("amocrm_get_contact", "Bitta kontakt ma'lumotlari.", "/contacts/{contact_id}", {
            "type": "object", "required": ["contact_id"], "properties": {
                "contact_id": {"type": "number", "description": "Kontakt ID"},
            }}, "contact_id")

        # Create contact
        async def create_contact(p: dict) -> str:
            try:
                body: dict[str, Any] = {"name": p.get("name", "")}
                custom_fields: list = []
                if p.get("phone"):
                    custom_fields.append({
                        "field_code": "PHONE",
                        "values": [{"value": p["phone"], "enum_code": "WORK"}],
                    })
                if p.get("email"):
                    custom_fields.append({
                        "field_code": "EMAIL",
                        "values": [{"value": p["email"], "enum_code": "WORK"}],
                    })
                if p.get("custom_fields_values"):
                    custom_fields.extend(p["custom_fields_values"])
                if custom_fields:
                    body["custom_fields_values"] = custom_fields
                if p.get("responsible_user_id") is not None:
                    body["responsible_user_id"] = p["responsible_user_id"]
                return self._ok(await c.post("/contacts", [body]))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("amocrm_create_contact", "Yangi kontakt yaratish.", {
            "type": "object", "required": ["name"], "properties": {
                "name": {"type": "string", "description": "Kontakt ismi"},
                "phone": {"type": "string", "description": "Telefon raqam (+998...)"},
                "email": {"type": "string", "description": "Email manzil"},
                "responsible_user_id": {"type": "number", "description": "Mas'ul shaxs ID"},
                "custom_fields_values": {"type": "array", "description": "Qo'shimcha fieldlar"},
            }}, create_contact))

        # ── PIPELINES (Voronkalar) ──
        _simple("amocrm_get_pipelines", "Barcha voronkalar va bosqichlari.", "/leads/pipelines", {
            "type": "object", "properties": {}})

        # ── TASKS (Vazifalar) ──
        _simple("amocrm_get_tasks", "Vazifalar ro'yxati.", "/tasks", {
            "type": "object", "properties": {
                "page": {"type": "number"},
                "limit": {"type": "number"},
                "responsible_user_id": {"type": "number", "description": "Mas'ul shaxs ID"},
                "is_completed": {"type": "number", "description": "0 — bajarilmagan, 1 — bajarilgan"},
            }})

        # Create task
        async def create_task(p: dict) -> str:
            try:
                body: dict[str, Any] = {
                    "text": p.get("text", ""),
                    "complete_till": p.get("complete_till", 0),
                    "task_type_id": p.get("task_type_id", 1),
                }
                if p.get("entity_id") is not None:
                    body["entity_id"] = p["entity_id"]
                if p.get("entity_type"):
                    body["entity_type"] = p["entity_type"]
                if p.get("responsible_user_id") is not None:
                    body["responsible_user_id"] = p["responsible_user_id"]
                return self._ok(await c.post("/tasks", [body]))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("amocrm_create_task", "Yangi vazifa yaratish.", {
            "type": "object", "required": ["text", "complete_till"], "properties": {
                "text": {"type": "string", "description": "Vazifa matni"},
                "complete_till": {"type": "number", "description": "Muddat (Unix timestamp)"},
                "task_type_id": {"type": "number", "description": "Vazifa turi ID (1 = qo'ng'iroq)"},
                "entity_id": {"type": "number", "description": "Bog'langan entity ID (lid/kontakt)"},
                "entity_type": {"type": "string", "description": "Entity turi: leads yoki contacts"},
                "responsible_user_id": {"type": "number", "description": "Mas'ul shaxs ID"},
            }}, create_task))

        # ── NOTES (Izohlar) ──
        async def add_note(p: dict) -> str:
            try:
                entity_type = p.get("entity_type", "leads")
                entity_id = p.get("entity_id")
                if not entity_id:
                    return self._err("entity_id majburiy")
                body = [{"note_type": "common", "params": {"text": p.get("text", "")}}]
                return self._ok(await c.post(f"/{entity_type}/{entity_id}/notes", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("amocrm_add_note", "Lid yoki kontaktga izoh qo'shish.", {
            "type": "object", "required": ["entity_id", "text"], "properties": {
                "entity_id": {"type": "number", "description": "Lid yoki kontakt ID"},
                "entity_type": {"type": "string", "description": "leads yoki contacts (default: leads)"},
                "text": {"type": "string", "description": "Izoh matni"},
            }}, add_note))

        # ── USERS (Foydalanuvchilar) ──
        _simple("amocrm_get_users", "CRM foydalanuvchilari (menejerlar) ro'yxati.", "/users", {
            "type": "object", "properties": {
                "page": {"type": "number"}, "limit": {"type": "number"},
            }})

        # ── EVENTS (Hodisalar) ──
        async def get_events(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if p.get("page"): params["page"] = p["page"]
                if p.get("limit"): params["limit"] = p["limit"]
                if p.get("event_type"): params["filter[type]"] = p["event_type"]
                return self._ok(await c.get("/events", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("amocrm_get_events", "So'nggi hodisalar ro'yxati.", {
            "type": "object", "properties": {
                "page": {"type": "number"}, "limit": {"type": "number"},
                "event_type": {"type": "string", "description": "Hodisa turi: incoming_chat_message, outgoing_chat_message, lead_added, lead_status_changed"},
            }}, get_events))

        # ── TALKS (Chatlar) ──
        async def get_talks(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if p.get("limit"): params["limit"] = p["limit"]
                if p.get("page"): params["page"] = p["page"]
                if p.get("is_read") is not None: params["filter[is_read]"] = str(p["is_read"]).lower()
                if p.get("status"): params["filter[status]"] = p["status"]
                return self._ok(await c.get("/talks", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("amocrm_get_talks", "Chatlar (suhbatlar) ro'yxati. Mijozlar bilan yozishmalar.", {
            "type": "object", "properties": {
                "limit": {"type": "number", "description": "Natijalar soni (max 250)"},
                "page": {"type": "number"},
                "is_read": {"type": "boolean", "description": "O'qilgan (true) yoki o'qilmagan (false)"},
                "status": {"type": "string", "description": "Holat: opened yoki in_work"},
            }}, get_talks))
        _simple("amocrm_get_talk", "Bitta chat tafsilotlari.", "/talks/{talk_id}", {
            "type": "object", "required": ["talk_id"], "properties": {
                "talk_id": {"type": "number", "description": "Chat ID"},
            }}, "talk_id")

        # Chat messages via events (incoming + outgoing)
        async def get_chat_messages(p: dict) -> str:
            """Get chat messages for a contact or lead using events API."""
            try:
                all_messages: list = []
                for msg_type in ("incoming_chat_message", "outgoing_chat_message"):
                    params: dict[str, Any] = {
                        "limit": p.get("limit", 50),
                        "filter[type]": msg_type,
                    }
                    if p.get("entity_id"):
                        params["filter[entity]"] = p["entity_id"]
                    if p.get("entity_type"):
                        params["filter[entity_type]"] = p["entity_type"]
                    data = await c.get("/events", params)
                    embedded = data.get("_embedded", {}) if isinstance(data, dict) else {}
                    events = embedded.get("events", [])
                    for ev in events:
                        val = ev.get("value_after", [{}])
                        msg_info = val[0].get("message", {}) if val else {}
                        all_messages.append({
                            "type": "kiruvchi" if "incoming" in ev.get("type", "") else "chiquvchi",
                            "vaqt": ev.get("created_at", 0),
                            "entity_id": ev.get("entity_id"),
                            "manba": msg_info.get("origin", ""),
                            "talk_id": msg_info.get("talk_id"),
                            "message_id": msg_info.get("id", ""),
                        })
                # Sort by time
                all_messages.sort(key=lambda m: m["vaqt"])
                return self._ok({
                    "jami_xabarlar": len(all_messages),
                    "xabarlar": all_messages,
                })
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("amocrm_get_chat_messages",
            "Chatdagi xabarlar tarixi — kim qachon yozgani. Lid yoki kontakt bo'yicha filter.", {
            "type": "object", "properties": {
                "entity_id": {"type": "number", "description": "Lid yoki kontakt ID"},
                "entity_type": {"type": "string", "description": "leads yoki contacts"},
                "limit": {"type": "number", "description": "Har bir tur uchun max xabarlar (default 50)"},
            }}, get_chat_messages))

        # Unread chats summary
        async def unread_chats(p: dict) -> str:
            """Get summary of unread chats."""
            try:
                data = await c.get("/talks", {"filter[is_read]": "false", "limit": 250})
                embedded = data.get("_embedded", {}) if isinstance(data, dict) else {}
                talks = embedded.get("talks", [])
                result: list = []
                for talk in talks:
                    contact_id = talk.get("contact_id")
                    # Fetch contact name
                    contact_name = ""
                    if contact_id:
                        try:
                            contact_data = await c.get(f"/contacts/{contact_id}")
                            contact_name = contact_data.get("name", "") if isinstance(contact_data, dict) else ""
                        except Exception:
                            pass
                    result.append({
                        "talk_id": talk.get("talk_id"),
                        "kontakt": contact_name or f"ID:{contact_id}",
                        "manba": talk.get("origin", ""),
                        "lid_id": talk.get("entity_id"),
                        "yangilangan": talk.get("updated_at", 0),
                    })
                return self._ok({
                    "oqilmagan_chatlar_soni": len(result),
                    "chatlar": result,
                })
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("amocrm_get_unread_chats",
            "O'qilmagan chatlar — javob kutayotgan mijozlar ro'yxati.", {
            "type": "object", "properties": {}}, unread_chats))

        return tools
