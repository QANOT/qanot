"""Eskiz SMS gateway plugin — xabar yuborish, holat, balans, hisobotlar."""

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


class EskizClient:
    """HTTP client for Eskiz SMS API with JWT auto-refresh."""

    BASE_URL = "https://notify.eskiz.uz/api"

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.token: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._rate_limiter = asyncio.Semaphore(5)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def authenticate(self) -> bool:
        """Login and get JWT token (valid 30 days)."""
        try:
            session = await self._get_session()
            data = aiohttp.FormData()
            data.add_field("email", self.email)
            data.add_field("password", self.password)
            async with session.post(
                f"{self.BASE_URL}/auth/login", data=data,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("[eskiz] Login failed (%d): %s", resp.status, text[:200])
                    return False
                result = await resp.json()
                token_data = result.get("data", result)
                self.token = token_data.get("token", "")
                if not self.token:
                    logger.error("[eskiz] No token in response: %s", str(result)[:200])
                    return False
                logger.info("[eskiz] Authenticated successfully")
                return True
        except Exception as e:
            logger.error("[eskiz] Login error: %s", e)
            return False

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: Any = None, form: dict | None = None) -> Any:
        return await self._request("POST", path, body=body, form=form)

    async def _request(self, method: str, path: str,
                       body: Any = None, params: dict | None = None,
                       form: dict | None = None, _retry: bool = True) -> Any:
        async with self._rate_limiter:
            session = await self._get_session()
            url = f"{self.BASE_URL}/{path.lstrip('/')}"
            headers: dict[str, str] = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"

            clean_params = {k: str(v) for k, v in (params or {}).items()
                           if v is not None and v != ""} or None

            kwargs: dict[str, Any] = {"headers": headers}
            if clean_params:
                kwargs["params"] = clean_params

            if form:
                fd = aiohttp.FormData()
                for k, v in form.items():
                    if v is not None and v != "":
                        fd.add_field(k, str(v))
                kwargs["data"] = fd
            elif body and method != "GET":
                kwargs["json"] = body
                headers["Content-Type"] = "application/json"

            async with session.request(method, url, **kwargs) as resp:
                if resp.status == 401 and _retry:
                    if await self.authenticate():
                        return await self._request(method, path, body, params, form, _retry=False)
                    raise RuntimeError("Eskiz autentifikatsiya xatosi")

                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After", "30")
                    raise RuntimeError(f"Juda ko'p so'rov — {retry_after}s kuting")

                if resp.status >= 500:
                    raise RuntimeError(f"Eskiz server xatosi ({resp.status})")

                if resp.content_type == "application/json":
                    data = await resp.json()
                else:
                    raw = await resp.text()
                    if "<html" in raw.lower():
                        raise RuntimeError("Eskiz API javob bermadi")
                    return {"_raw": raw}

                if isinstance(data, dict) and data.get("status") == "error":
                    raise RuntimeError(data.get("message", "Eskiz xatosi"))

                return data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class QanotPlugin(Plugin):
    """Eskiz SMS gateway plugin."""

    name = "eskiz"
    description = "Eskiz SMS — xabar yuborish, holat tekshirish, balans, hisobotlar"
    tools_md = TOOLS_MD
    soul_append = SOUL_APPEND

    def __init__(self):
        self.client: EskizClient | None = None

    async def setup(self, config: dict) -> None:
        email = config.get("email", "")
        password = config.get("password", "")
        if not all([email, password]):
            logger.warning("[eskiz] Missing config (email, password)")
            return
        self.client = EskizClient(email=email, password=password)
        if not await self.client.authenticate():
            logger.error("[eskiz] Initial login failed — plugin disabled")
            self.client = None
            return
        logger.info("[eskiz] Plugin ready")

    async def teardown(self) -> None:
        if self.client:
            await self.client.close()

    def get_tools(self) -> list[ToolDef]:
        if not self.client:
            return []
        tools = self._build_api_tools()
        logger.info("[eskiz] %d tools registered", len(tools))
        return tools

    def _ok(self, data: Any) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _err(self, msg: str) -> str:
        return json.dumps({"error": msg})

    def _build_api_tools(self) -> list[ToolDef]:
        c = self.client
        assert c is not None
        tools: list[ToolDef] = []

        # ═══════════════════════════════════════
        # SMS YUBORISH (Sending) — 3 tools
        # ═══════════════════════════════════════

        async def send_sms(p: dict) -> str:
            try:
                form = {
                    "mobile_phone": p["phone"],
                    "message": p["message"],
                    "from": p.get("from", "4546"),
                }
                if p.get("callback_url"):
                    form["callback_url"] = p["callback_url"]
                return self._ok(await c.post("message/sms/send", form=form))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_send_sms",
                             "SMS xabar yuborish. Telefon raqam va matn kiritiladi.",
                             {"type": "object", "required": ["phone", "message"], "properties": {
                                 "phone": {"type": "string", "description": "Telefon raqam (998901234567 formatda)"},
                                 "message": {"type": "string", "description": "Xabar matni"},
                                 "from": {"type": "string", "description": "Yuboruvchi nomi (alpha-name). Default: 4546"},
                                 "callback_url": {"type": "string", "description": "Yetkazish holatini yuborish URL (webhook)"},
                             }}, send_sms))

        async def send_batch(p: dict) -> str:
            try:
                messages = p["messages"]
                body = {
                    "messages": [
                        {"user_sms_id": str(i), "to": m["phone"], "text": m["message"]}
                        for i, m in enumerate(messages)
                    ],
                    "from": p.get("from", "4546"),
                }
                if p.get("dispatch_id"):
                    body["dispatch_id"] = p["dispatch_id"]
                return self._ok(await c.post("message/sms/send-batch", body=body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_send_batch",
                             "Bir nechta SMS yuborish (ommaviy). Xabarlar ro'yxati kiritiladi.",
                             {"type": "object", "required": ["messages"], "properties": {
                                 "messages": {"type": "array", "description": "Xabarlar: [{phone, message}]",
                                              "items": {"type": "object", "properties": {
                                                  "phone": {"type": "string"},
                                                  "message": {"type": "string"},
                                              }}},
                                 "from": {"type": "string", "description": "Yuboruvchi nomi"},
                                 "dispatch_id": {"type": "string", "description": "Kampaniya ID (ixtiyoriy)"},
                             }}, send_batch))

        async def check_message(p: dict) -> str:
            try:
                return self._ok(await c.post("message/sms/normalizer", form={"message": p["message"]}))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_check_message",
                             "Xabar matnini tekshirish — maxsus belgilar, uzunlik, optimizatsiya.",
                             {"type": "object", "required": ["message"], "properties": {
                                 "message": {"type": "string", "description": "Tekshiriladigan matn"},
                             }}, check_message))

        # ═══════════════════════════════════════
        # HOLAT (Status) — 2 tools
        # ═══════════════════════════════════════

        async def get_sms_status(p: dict) -> str:
            try:
                return self._ok(await c.get(f"message/sms/status_by_id/{p['id']}"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_get_sms_status",
                             "SMS yetkazish holatini tekshirish (ID bo'yicha).",
                             {"type": "object", "required": ["id"], "properties": {
                                 "id": {"type": "string", "description": "SMS ID yoki request UUID"},
                             }}, get_sms_status))

        async def get_dispatch_status(p: dict) -> str:
            try:
                return self._ok(await c.post("message/sms/get-dispatch-status",
                                             form={"dispatch_id": p["dispatch_id"]}))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_get_dispatch_status",
                             "Kampaniya (dispatch) yetkazish statistikasi.",
                             {"type": "object", "required": ["dispatch_id"], "properties": {
                                 "dispatch_id": {"type": "string", "description": "Kampaniya ID"},
                             }}, get_dispatch_status))

        # ═══════════════════════════════════════
        # TARIX (History) — 1 tool
        # ═══════════════════════════════════════

        async def get_messages(p: dict) -> str:
            try:
                form: dict[str, Any] = {"page_size": p.get("limit", 20)}
                if p.get("start_date"):
                    form["start_date"] = p["start_date"]
                if p.get("to_date"):
                    form["to_date"] = p["to_date"]
                if p.get("is_ad") is not None:
                    form["is_ad"] = "1" if p["is_ad"] else "0"
                params = {}
                if p.get("status"):
                    params["status"] = p["status"]
                return self._ok(await c.post("message/sms/get-user-messages",
                                             form=form, params=params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_get_messages",
                             "Yuborilgan xabarlar tarixi. Sana va holat bo'yicha filter.",
                             {"type": "object", "properties": {
                                 "start_date": {"type": "string", "description": "Boshlanish (YYYY-MM-DD HH:MM)"},
                                 "to_date": {"type": "string", "description": "Tugash (YYYY-MM-DD HH:MM)"},
                                 "status": {"type": "string", "description": "Holat: all, delivered, rejected"},
                                 "is_ad": {"type": "boolean", "description": "Faqat reklama xabarlar"},
                                 "limit": {"type": "number", "description": "Natijalar soni (20-200)"},
                             }}, get_messages))

        # ═══════════════════════════════════════
        # AKKAUNT (Account) — 3 tools
        # ═══════════════════════════════════════

        async def get_balance(p: dict) -> str:
            try:
                return self._ok(await c.get("user/get-limit"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_get_balance",
                             "SMS balans — qolgan kredit va limit.",
                             {"type": "object", "properties": {}}, get_balance))

        async def get_user_info(p: dict) -> str:
            try:
                return self._ok(await c.get("auth/user"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_get_user_info",
                             "Akkaunt ma'lumotlari — ism, email, balans, holat.",
                             {"type": "object", "properties": {}}, get_user_info))

        async def get_nicknames(p: dict) -> str:
            try:
                return self._ok(await c.get("nick/me"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_get_nicknames",
                             "Mavjud yuboruvchi nomlari (alpha-name) ro'yxati.",
                             {"type": "object", "properties": {}}, get_nicknames))

        # ═══════════════════════════════════════
        # SHABLONLAR (Templates) — 1 tool
        # ═══════════════════════════════════════

        async def get_templates(p: dict) -> str:
            try:
                return self._ok(await c.get("user/templates"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_get_templates",
                             "SMS shablonlar ro'yxati va ularning holati.",
                             {"type": "object", "properties": {}}, get_templates))

        # ═══════════════════════════════════════
        # HISOBOTLAR (Reports) — 3 tools
        # ═══════════════════════════════════════

        async def get_totals(p: dict) -> str:
            try:
                form = {
                    "month": p.get("month", ""),
                    "year": p.get("year", ""),
                }
                return self._ok(await c.post("user/totals", form=form))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_get_totals",
                             "Umumiy SMS statistikasi — yuborilgan, sarflangan.",
                             {"type": "object", "properties": {
                                 "month": {"type": "number", "description": "Oy (1-12)"},
                                 "year": {"type": "number", "description": "Yil (masalan: 2026)"},
                             }}, get_totals))

        async def get_monthly_report(p: dict) -> str:
            try:
                params = {"year": p.get("year", "2026")}
                return self._ok(await c.get("report/total-by-month", params=params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_get_monthly_report",
                             "Oylik xarajatlar hisoboti — har oy uchun sarf.",
                             {"type": "object", "properties": {
                                 "year": {"type": "number", "description": "Yil (default: 2026)"},
                             }}, get_monthly_report))

        async def get_range_report(p: dict) -> str:
            try:
                form: dict[str, Any] = {}
                if p.get("start_date"):
                    form["start_date"] = p["start_date"]
                if p.get("to_date"):
                    form["to_date"] = p["to_date"]
                params = {}
                if p.get("status"):
                    params["status"] = p["status"]
                return self._ok(await c.post("report/total-by-range",
                                             form=form, params=params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef("eskiz_get_range_report",
                             "Sana oralig'idagi xarajatlar hisoboti.",
                             {"type": "object", "properties": {
                                 "start_date": {"type": "string", "description": "Boshlanish (YYYY-MM-DD HH:MM)"},
                                 "to_date": {"type": "string", "description": "Tugash (YYYY-MM-DD HH:MM)"},
                                 "status": {"type": "string", "description": "Holat: all, delivered, rejected"},
                             }}, get_range_report))

        return tools
