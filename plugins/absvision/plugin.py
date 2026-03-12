"""AbsVision HR plugin — 25 HR tools with OAuth refresh token support."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from qanot.plugins.base import Plugin, ToolDef

logger = logging.getLogger(__name__)

TOOLS_MD = (Path(__file__).parent / "TOOLS.md").read_text(encoding="utf-8") if (Path(__file__).parent / "TOOLS.md").exists() else ""
SOUL_APPEND = (Path(__file__).parent / "SOUL_APPEND.md").read_text(encoding="utf-8") if (Path(__file__).parent / "SOUL_APPEND.md").exists() else ""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class AbsVisionClient:
    """HTTP client for AbsVision HR API with refresh token support."""

    def __init__(self, base_url: str, phone: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.phone = phone
        self.password = password
        self.token: str | None = None
        self.refresh_token: str | None = None
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def login(self) -> None:
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/api/v1/auth/login",
            json={"phone": self.phone, "password": self.password},
            headers={"Content-Type": "application/json"},
        ) as resp:
            res = await resp.json()
        data = res.get("data", res) if isinstance(res, dict) else res
        token = data.get("access_token") or data.get("token") if isinstance(data, dict) else None
        if not token:
            raise RuntimeError(f"Login failed: {json.dumps(res)[:200]}")
        self.token = token
        self.refresh_token = data.get("refresh_token") if isinstance(data, dict) else None

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: dict | None = None) -> Any:
        return await self._request("POST", path, body=body)

    async def get_all(self, path: str, params: dict | None = None, max_items: int = 5000) -> dict:
        """Fetch all pages using skip/limit pagination."""
        all_items: list = []
        skip = 0
        limit = 100
        total = 0
        while len(all_items) < max_items:
            data = await self.get(path, {**(params or {}), "skip": skip, "limit": limit})
            items = data.get("items", data.get("data", [])) if isinstance(data, dict) else []
            if not isinstance(items, list) or not items:
                total = data.get("total", len(all_items)) if isinstance(data, dict) else len(all_items)
                break
            all_items.extend(items)
            total = data.get("total", len(all_items)) if isinstance(data, dict) else len(all_items)
            if len(items) < limit:
                break
            skip += limit
        return {"items": all_items, "total": total}

    async def _request(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any:
        res = await self._raw(method, path, body, params)
        status = res.pop("_status", 200) if isinstance(res, dict) else 200
        if status in (401, 403):
            # Try refresh
            if self.refresh_token:
                try:
                    refresh_res = await self._raw("POST", "/api/v1/auth/refresh", {"refresh_token": self.refresh_token})
                    rd = refresh_res.get("data", refresh_res) if isinstance(refresh_res, dict) else {}
                    if isinstance(rd, dict) and rd.get("access_token"):
                        self.token = rd["access_token"]
                        self.refresh_token = rd.get("refresh_token", self.refresh_token)
                        retry = await self._raw(method, path, body, params)
                        retry.pop("_status", None)
                        return retry.get("data", retry) if isinstance(retry, dict) else retry
                except Exception:
                    pass
            # Fallback: re-login
            await self.login()
            retry = await self._raw(method, path, body, params)
            retry.pop("_status", None)
            return retry.get("data", retry) if isinstance(retry, dict) else retry
        if status >= 400:
            detail = res.get("detail", res.get("message", f"HTTP {status}")) if isinstance(res, dict) else f"HTTP {status}"
            raise RuntimeError(str(detail))
        return res.get("data", res) if isinstance(res, dict) else res

    async def _raw(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None and v != ""}
        async with session.request(method, url, json=body if method != "GET" else None, params=clean_params, headers=headers) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = {"_raw": await resp.text()}
            if isinstance(data, dict):
                data["_status"] = resp.status
            return data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class QanotPlugin(Plugin):
    """AbsVision HR plugin."""

    name = "absvision"
    description = "AbsVision HR — xodimlar, davomat, ta'tillar, oshxona"
    tools_md = TOOLS_MD
    soul_append = SOUL_APPEND

    def __init__(self):
        self.client: AbsVisionClient | None = None

    async def setup(self, config: dict) -> None:
        api_url = config.get("api_url", "")
        phone = config.get("phone", "")
        password = config.get("password", "")
        if not api_url or not phone or not password:
            logger.warning("[absvision] Missing config (api_url, phone, password)")
            return
        self.client = AbsVisionClient(api_url, phone, password)
        try:
            await self.client.login()
            logger.info("[absvision] Logged in successfully")
        except Exception as e:
            logger.error("[absvision] Login failed: %s", e)
            self.client = None

    async def teardown(self) -> None:
        if self.client:
            await self.client.close()

    def get_tools(self) -> list[ToolDef]:
        if not self.client:
            return []
        c = self.client
        tools: list[ToolDef] = []

        def _ok(data: Any) -> str:
            return json.dumps(data, indent=2, ensure_ascii=False)

        def _err(msg: str) -> str:
            return json.dumps({"error": msg})

        def _simple(name: str, desc: str, path: str, schema: dict, path_param: str | None = None):
            async def handler(p: dict, _path=path, _pk=path_param) -> str:
                try:
                    actual = _path
                    if _pk and _pk in p:
                        actual = _path.replace(f"{{{_pk}}}", str(p.pop(_pk)))
                    return _ok(await c.get(actual, p if p else None))
                except Exception as e:
                    return _err(str(e))
            tools.append(ToolDef(name=name, description=desc, parameters=schema, handler=handler))

        # ── EMPLOYEES ──
        _simple("absvision_get_employees", "Xodimlar ro'yxati. Bo'lim, holat, qidiruv bo'yicha filter.", "/api/v1/employees", {
            "type": "object", "properties": {
                "department_id": {"type": "string"}, "is_active": {"type": "boolean"},
                "search": {"type": "string"}, "skip": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absvision_get_employee", "Bitta xodim tafsilotlari.", "/api/v1/employees/{user_id}", {
            "type": "object", "required": ["user_id"], "properties": {"user_id": {"type": "string"}}}, "user_id")
        _simple("absvision_get_employee_options", "Xodimlar dropdown ro'yxati.", "/api/v1/employees/options", {
            "type": "object", "properties": {"department_id": {"type": "string"}, "search": {"type": "string"}}})

        # ── DEPARTMENTS & POSITIONS ──
        _simple("absvision_get_departments", "Bo'limlar ro'yxati.", "/api/v1/departments", {
            "type": "object", "properties": {"is_active": {"type": "boolean"}, "search": {"type": "string"}}})
        _simple("absvision_get_positions", "Lavozimlar ro'yxati.", "/api/v1/positions", {
            "type": "object", "properties": {"department_id": {"type": "string"}, "is_active": {"type": "boolean"}, "search": {"type": "string"}}})
        _simple("absvision_get_branches", "Filiallar ro'yxati.", "/api/v1/branches/options", {
            "type": "object", "properties": {"search": {"type": "string"}}})

        # ── ATTENDANCE ──
        async def attendance_dashboard(p: dict) -> str:
            try:
                if not p.get("date"):
                    p["date"] = _today()
                return _ok(await c.get("/api/v1/attendance/management/dashboard", p))
            except Exception as e:
                return _err(str(e))
        tools.append(ToolDef("absvision_attendance_dashboard",
            "Davomat boshqaruv paneli — bugungi holat: kelganlar, kechikkanlar, yo'qlar.", {
            "type": "object", "properties": {
                "date": {"type": "string"}, "status": {"type": "string"},
                "department_id": {"type": "string"}, "search": {"type": "string"},
                "skip": {"type": "number"}, "limit": {"type": "number"},
            }}, attendance_dashboard))

        _simple("absvision_attendance_logs", "Davomat loglari — kirish/chiqish.", "/api/v1/attendance/management/logs", {
            "type": "object", "properties": {
                "user_id": {"type": "string"}, "start_date": {"type": "string"},
                "end_date": {"type": "string"}, "schedule_date": {"type": "string"},
                "skip": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absvision_attendance_summaries", "Kunlik davomat xulosalari.", "/api/v1/attendance/management/summaries", {
            "type": "object", "properties": {
                "user_id": {"type": "string"}, "status": {"type": "string"},
                "start_date": {"type": "string"}, "end_date": {"type": "string"},
                "skip": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absvision_attendance_team_summary", "Jamoa davomat xulosasi.", "/api/v1/attendance/management/team-summary", {
            "type": "object", "required": ["start_date", "end_date"], "properties": {
                "start_date": {"type": "string"}, "end_date": {"type": "string"},
            }})
        _simple("absvision_attendance_inout_stats", "Kirish-chiqish statistikasi.", "/api/v1/attendance/management/statistics/in-out", {
            "type": "object", "required": ["start_date", "end_date"], "properties": {
                "start_date": {"type": "string"}, "end_date": {"type": "string"}, "user_id": {"type": "string"},
            }})
        _simple("absvision_attendance_audit", "Davomat audit.", "/api/v1/attendance/management/audit", {
            "type": "object", "required": ["date"], "properties": {
                "date": {"type": "string"}, "audit_type": {"type": "string"}, "department_id": {"type": "string"},
            }})
        _simple("absvision_attendance_monthly", "Oylik davomat statistikasi.", "/api/v1/attendance/stats/monthly", {
            "type": "object", "properties": {"year": {"type": "number"}, "month": {"type": "number"}}})
        _simple("absvision_attendance_today", "Bugungi davomat holati.", "/api/v1/attendance/today", {
            "type": "object", "properties": {}})

        # ── LEAVES ──
        _simple("absvision_leave_types", "Ta'til turlari.", "/api/v1/leave-management/types", {
            "type": "object", "properties": {"include_inactive": {"type": "boolean"}}})
        _simple("absvision_leave_requests", "Ta'til so'rovlari ro'yxati.", "/api/v1/leave-management/requests", {
            "type": "object", "properties": {
                "department_id": {"type": "string"}, "user_id": {"type": "string"},
                "status": {"type": "string"}, "skip": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absvision_leave_request_detail", "Bitta ta'til so'rovi.", "/api/v1/leave-management/requests/{request_id}", {
            "type": "object", "required": ["request_id"], "properties": {"request_id": {"type": "string"}}}, "request_id")
        _simple("absvision_leave_balances", "Ta'til qoldiqlari.", "/api/v1/my-leave/balances", {
            "type": "object", "properties": {"year": {"type": "number"}}})
        _simple("absvision_holidays", "Bayramlar ro'yxati.", "/api/v1/my-leave/holidays", {
            "type": "object", "properties": {"year": {"type": "number"}}})

        # ── CANTEEN ──
        _simple("absvision_canteen_menu", "Oshxona menyu.", "/api/v1/canteen/menu", {
            "type": "object", "properties": {"date": {"type": "string"}}})
        _simple("absvision_canteen_transactions", "Oshxona tranzaksiyalari.", "/api/v1/canteen/my-transactions", {
            "type": "object", "properties": {
                "start_date": {"type": "string"}, "end_date": {"type": "string"},
                "skip": {"type": "number"}, "limit": {"type": "number"},
            }})
        _simple("absvision_canteen_monthly", "Oshxona oylik xulosa.", "/api/v1/canteen/my-summary/monthly", {
            "type": "object", "properties": {"year": {"type": "number"}, "month": {"type": "number"}}})

        # ── PROFILE ──
        _simple("absvision_my_dashboard", "Shaxsiy dashboard.", "/api/v1/me/dashboard", {
            "type": "object", "properties": {}})
        _simple("absvision_my_profile", "Profil ma'lumotlari.", "/api/v1/me/profile", {
            "type": "object", "properties": {}})

        # ── CAREER & DOCUMENTS ──
        _simple("absvision_employee_career", "Xodim karyera tarixi.", "/api/v1/employees/{user_id}/career-history", {
            "type": "object", "required": ["user_id"], "properties": {"user_id": {"type": "string"}}}, "user_id")

        async def employee_documents(p: dict) -> str:
            try:
                uid = p.pop("user_id")
                return _ok(await c.get(f"/api/v1/employees/{uid}/documents", {"document_type": p.get("document_type")}))
            except Exception as e:
                return _err(str(e))
        tools.append(ToolDef("absvision_employee_documents", "Xodim hujjatlari.", {
            "type": "object", "required": ["user_id"], "properties": {
                "user_id": {"type": "string"}, "document_type": {"type": "string"},
            }}, employee_documents))

        # ── EXPORTS ──
        _simple("absvision_export_attendance", "Davomat eksport (XLSX/CSV).", "/api/v1/attendance/management/export", {
            "type": "object", "required": ["start_date", "end_date"], "properties": {
                "start_date": {"type": "string"}, "end_date": {"type": "string"},
                "user_id": {"type": "string"}, "format": {"type": "string"},
            }})
        _simple("absvision_export_employees", "Xodimlar eksport.", "/api/v1/employees/export", {
            "type": "object", "properties": {
                "format": {"type": "string"}, "department_id": {"type": "string"},
                "is_active": {"type": "boolean"}, "include_salary": {"type": "boolean"},
                "include_contact": {"type": "boolean"},
            }})

        logger.info("[absvision] %d tools registered", len(tools))
        return tools
