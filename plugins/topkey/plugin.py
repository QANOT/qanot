"""TopKey HR + Project Management plugin (28 tools)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from qanot.plugins.base import Plugin, ToolDef
from qanot.secrets import resolve_secret

logger = logging.getLogger(__name__)


def _import_client():
    """Import the HTTP client lazily.

    Plugin's own dir is added to sys.path by the loader before plugin.py is
    executed, but tests that `from plugins.topkey.plugin import QanotPlugin`
    don't go through the loader. Doing the import here covers both paths and
    avoids putting `tk_engine` on sys.path globally at module load time.
    """
    import sys
    plugin_dir = str(Path(__file__).parent)
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    from tk_engine.client import TopKeyAPIError, TopKeyAuthError, TopKeyClient
    return TopKeyClient, TopKeyAuthError, TopKeyAPIError

_PLUGIN_DIR = Path(__file__).parent
TOOLS_MD = (_PLUGIN_DIR / "TOOLS.md").read_text(encoding="utf-8") if (_PLUGIN_DIR / "TOOLS.md").exists() else ""
SOUL_APPEND = (_PLUGIN_DIR / "SOUL_APPEND.md").read_text(encoding="utf-8") if (_PLUGIN_DIR / "SOUL_APPEND.md").exists() else ""


class QanotPlugin(Plugin):
    """TopKey HR + PM plugin."""

    name = "topkey"
    description = "TopKey HR & Project Management — xodimlar, davomat, ta'tillar, loyihalar, vazifalar"
    tools_md = TOOLS_MD
    soul_append = SOUL_APPEND

    def __init__(self):
        self.client: Any = None
        self._workspace_dir: str = ""

    async def setup(self, config: dict) -> None:
        api_url = config.get("api_url", "")
        email = config.get("email", "")
        # Password may be plain or a SecretRef ({"env": "..."}/{"file": "..."}).
        # The framework only auto-resolves a fixed set of top-level secret
        # fields (api_key, bot_token, ...) — plugin configs are passed through
        # untouched, so the plugin resolves its own SecretRef shapes.
        try:
            password = resolve_secret(config.get("password", ""))
        except Exception as e:
            logger.warning("[topkey] Password resolution failed: %s", e)
            password = ""
        self._workspace_dir = config.get("workspace_dir", "")
        if not api_url or not email or not password:
            logger.warning("[topkey] Missing config (api_url, email, password) — plugin disabled")
            return
        TopKeyClient, TopKeyAuthError, _ = _import_client()
        self.client = TopKeyClient(api_url, email, password)
        try:
            await self.client.login()
            logger.info("[topkey] Logged in successfully")
        except TopKeyAuthError as e:
            logger.error("[topkey] Login failed: %s", e)
            self.client = None
        except Exception as e:
            logger.error("[topkey] Unexpected error during login: %s", e)
            self.client = None

    async def teardown(self) -> None:
        if self.client:
            await self.client.close()

    def get_tools(self) -> list[ToolDef]:
        if not self.client:
            return []
        tools = self._build_tools()
        logger.info("[topkey] %d tools registered", len(tools))
        return tools

    # ── Helpers ───────────────────────────────────────────────

    def _ok(self, data: Any) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)

    def _err(self, msg: str) -> str:
        return json.dumps({"error": msg}, ensure_ascii=False)

    def _build_tools(self) -> list[ToolDef]:
        c = self.client
        assert c is not None
        tools: list[ToolDef] = []

        # ── HR / EMPLOYEES (5) ───────────────────────────────

        async def list_employees(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if "department_id" in p:
                    params["department_id"] = p["department_id"]
                if "designation_id" in p:
                    params["designation_id"] = p["designation_id"]
                if "status" in p:
                    params["status"] = p["status"]
                if "page" in p:
                    params["page"] = p["page"]
                if "per_page" in p:
                    params["per_page"] = p["per_page"]
                result = await c.get("/employee", params or None)
                return self._ok(result)
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_list_employees",
            "TopKey: Xodimlar ro'yxati. Filter: department_id, designation_id, status. Paginatsiya: page, per_page.",
            {"type": "object", "properties": {
                "department_id": {"type": "number"},
                "designation_id": {"type": "number"},
                "status": {"type": "string", "description": "active | deactive"},
                "page": {"type": "number"},
                "per_page": {"type": "number"},
            }},
            list_employees,
        ))

        async def get_employee(p: dict) -> str:
            if "employee_id" not in p:
                return self._err("employee_id is required")
            try:
                return self._ok(await c.get(f"/employee/{int(p['employee_id'])}"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_get_employee",
            "TopKey: Bitta xodimning to'liq profili (employee_id bo'yicha).",
            {"type": "object", "required": ["employee_id"], "properties": {
                "employee_id": {"type": "number"},
            }},
            get_employee,
        ))

        async def list_departments(_p: dict) -> str:
            try:
                return self._ok(await c.get("/department"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_list_departments",
            "TopKey: Bo'limlar ro'yxati.",
            {"type": "object", "properties": {}},
            list_departments,
        ))

        async def list_designations(_p: dict) -> str:
            try:
                return self._ok(await c.get("/designation"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_list_designations",
            "TopKey: Lavozimlar ro'yxati.",
            {"type": "object", "properties": {}},
            list_designations,
        ))

        async def get_user_profile(_p: dict) -> str:
            try:
                return self._ok(await c.get("/auth/me"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_get_user_profile",
            "TopKey: Joriy autentifikatsiya qilingan foydalanuvchi profili (\"men kim?\" savollari uchun).",
            {"type": "object", "properties": {}},
            get_user_profile,
        ))

        # ── HR / ATTENDANCE (5) ──────────────────────────────

        async def get_today_attendance(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if "date" in p:
                    params["date"] = p["date"]
                # /mobile/attendance/all is the admin "today across employees"
                # view; falls back to /attendance/today if `date` is absent.
                path = "/mobile/attendance/all" if params else "/attendance/today"
                return self._ok(await c.get(path, params or None))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_get_today_attendance",
            "TopKey: Bugun (yoki berilgan sanada) check-in qilgan barcha xodimlar (admin ko'rinishi).",
            {"type": "object", "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD; bo'sh bo'lsa bugungi sana"},
            }},
            get_today_attendance,
        ))

        async def get_user_attendance(p: dict) -> str:
            if "user_id" not in p:
                return self._err("user_id is required")
            try:
                params: dict[str, Any] = {"user_id": p["user_id"]}
                # employeeHistory uses year+month; allow both granularities.
                if "year" in p:
                    params["year"] = p["year"]
                if "month" in p:
                    params["month"] = p["month"]
                if "from_date" in p:
                    params["from_date"] = p["from_date"]
                if "to_date" in p:
                    params["to_date"] = p["to_date"]
                return self._ok(await c.get("/mobile/attendance/employee-history", params))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_get_user_attendance",
            "TopKey: Bitta xodim davomat tarixi (year+month yoki from_date+to_date oralig'ida).",
            {"type": "object", "required": ["user_id"], "properties": {
                "user_id": {"type": "number"},
                "year": {"type": "number"},
                "month": {"type": "number"},
                "from_date": {"type": "string"},
                "to_date": {"type": "string"},
            }},
            get_user_attendance,
        ))

        async def get_team_summary(p: dict) -> str:
            """Aggregate /mobile/attendance/all into present/absent/late/on_leave counts."""
            try:
                params: dict[str, Any] = {}
                if "date" in p:
                    params["date"] = p["date"]
                data = await c.get("/mobile/attendance/all", params or None)
                rows = []
                if isinstance(data, dict):
                    inner = data.get("data")
                    if isinstance(inner, list):
                        rows = inner
                    elif isinstance(inner, dict) and isinstance(inner.get("data"), list):
                        rows = inner["data"]
                elif isinstance(data, list):
                    rows = data
                present = absent = late = on_leave = 0
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    if str(r.get("is_on_leave", "0")) == "1":
                        on_leave += 1
                        continue
                    check_in = r.get("check_in", "-")
                    if check_in and check_in != "-":
                        present += 1
                        # Heuristic: a "late" flag isn't always present; rely on
                        # explicit field if backend supplies one.
                        if r.get("is_late") in (1, "1", True, "true"):
                            late += 1
                    else:
                        absent += 1
                return self._ok({
                    "date": params.get("date") or "today",
                    "total": len(rows),
                    "present": present,
                    "absent": absent,
                    "late": late,
                    "on_leave": on_leave,
                })
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_get_team_summary",
            "TopKey: Kunlik davomat sarhisobi (present, absent, late, on_leave hisoblari).",
            {"type": "object", "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD; bo'sh — bugun"},
            }},
            get_team_summary,
        ))

        async def get_late_arrivals(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if "date" in p:
                    params["date"] = p["date"]
                data = await c.get("/mobile/attendance/all", params or None)
                rows = []
                if isinstance(data, dict):
                    inner = data.get("data")
                    if isinstance(inner, list):
                        rows = inner
                elif isinstance(data, list):
                    rows = data
                late = [
                    r for r in rows
                    if isinstance(r, dict) and r.get("is_late") in (1, "1", True, "true")
                ]
                return self._ok({"date": params.get("date") or "today", "count": len(late), "employees": late})
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_get_late_arrivals",
            "TopKey: Berilgan sanada kech kelgan xodimlar.",
            {"type": "object", "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            }},
            get_late_arrivals,
        ))

        async def get_overtime(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if "user_id" in p:
                    params["user_id"] = p["user_id"]
                if "from_date" in p:
                    params["from_date"] = p["from_date"]
                if "to_date" in p:
                    params["to_date"] = p["to_date"]
                # Attendance index supports filters; overtime fields surface in
                # individual attendance records (overtime_hours, total_hours).
                return self._ok(await c.get("/attendance", params or None))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_get_overtime",
            "TopKey: Sverxurochniy hisobot — xodim va sana oralig'i bo'yicha (attendance recordlardagi overtime_hours).",
            {"type": "object", "properties": {
                "user_id": {"type": "number"},
                "from_date": {"type": "string"},
                "to_date": {"type": "string"},
            }},
            get_overtime,
        ))

        # ── HR / LEAVE (5) ───────────────────────────────────

        async def list_leave_requests(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if "status" in p:
                    params["status"] = p["status"]
                if "user_id" in p:
                    params["user_id"] = p["user_id"]
                if "from_date" in p:
                    params["from_date"] = p["from_date"]
                if "to_date" in p:
                    params["to_date"] = p["to_date"]
                if "page" in p:
                    params["page"] = p["page"]
                if "per_page" in p:
                    params["per_page"] = p["per_page"]
                return self._ok(await c.get("/leave", params or None))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_list_leave_requests",
            "TopKey: Ta'til so'rovlari ro'yxati. Filter: status (pending|approved|rejected), user_id, from_date/to_date.",
            {"type": "object", "properties": {
                "status": {"type": "string"},
                "user_id": {"type": "number"},
                "from_date": {"type": "string"},
                "to_date": {"type": "string"},
                "page": {"type": "number"},
                "per_page": {"type": "number"},
            }},
            list_leave_requests,
        ))

        async def create_leave_request(p: dict) -> str:
            for k in ("user_id", "leave_type_id", "start_date", "end_date"):
                if k not in p:
                    return self._err(f"{k} is required")
            try:
                body = {
                    "user_ids": [int(p["user_id"])],
                    "leave_type_id": int(p["leave_type_id"]),
                    "duration": p.get("duration", "multiple"),
                    "start_date": p["start_date"],
                    "end_date": p["end_date"],
                    "reason": p.get("reason", ""),
                }
                # /mobile/leave/grant is the admin "grant leave" endpoint that
                # accepts the multi-user shape; falls back to /leave/apply for
                # the single-user case.
                return self._ok(await c.post("/mobile/leave/grant", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_create_leave_request",
            "TopKey: Admin tomonidan ta'til berish (user_id, leave_type_id, start_date, end_date, reason).",
            {"type": "object", "required": ["user_id", "leave_type_id", "start_date", "end_date"], "properties": {
                "user_id": {"type": "number"},
                "leave_type_id": {"type": "number"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "duration": {"type": "string", "description": "single | multiple (default: multiple)"},
                "reason": {"type": "string"},
            }},
            create_leave_request,
        ))

        async def approve_leave(p: dict) -> str:
            if "leave_id" not in p:
                return self._err("leave_id is required")
            try:
                body = {"approve_reason": p.get("approve_reason", p.get("comment", ""))}
                return self._ok(await c.put(f"/mobile/leave/{int(p['leave_id'])}/approve", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_approve_leave",
            "TopKey: Ta'til so'rovini tasdiqlash (leave_id, ixtiyoriy approve_reason).",
            {"type": "object", "required": ["leave_id"], "properties": {
                "leave_id": {"type": "number"},
                "approve_reason": {"type": "string"},
            }},
            approve_leave,
        ))

        async def get_leave_balance(p: dict) -> str:
            if "user_id" not in p:
                return self._err("user_id is required")
            try:
                # No dedicated balance endpoint exists; the admin grant flow
                # surfaces remaining days in the user employeeDetail. For now
                # we return the user with leave types so the agent can compute
                # remaining = entitlement - used.
                user = await c.get(f"/employee/{int(p['user_id'])}")
                types = await c.get("/leave-type")
                return self._ok({"user": user, "leave_types": types})
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_get_leave_balance",
            "TopKey: Xodimning qolgan ta'til kunlari (turi bo'yicha).",
            {"type": "object", "required": ["user_id"], "properties": {
                "user_id": {"type": "number"},
            }},
            get_leave_balance,
        ))

        async def list_leave_types(_p: dict) -> str:
            try:
                return self._ok(await c.get("/leave-type"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_list_leave_types",
            "TopKey: Ta'til turlari ro'yxati.",
            {"type": "object", "properties": {}},
            list_leave_types,
        ))

        # ── WORK / PROJECTS (3) ──────────────────────────────

        async def list_projects(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if "status" in p:
                    params["status"] = p["status"]
                if "client_id" in p:
                    params["client_id"] = p["client_id"]
                if "category_id" in p:
                    params["category_id"] = p["category_id"]
                # Auto-walk pages so the agent doesn't loop.
                if p.get("all"):
                    return self._ok(await c.get_all("/project", params or None))
                if "page" in p:
                    params["page"] = p["page"]
                if "per_page" in p:
                    params["per_page"] = p["per_page"]
                return self._ok(await c.get("/project", params or None))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_list_projects",
            "TopKey: Loyihalar ro'yxati. Filter: status, client_id, category_id. all=true bo'lsa avto-paginatsiya (max 5 sahifa / 500 ta).",
            {"type": "object", "properties": {
                "status": {"type": "string"},
                "client_id": {"type": "number"},
                "category_id": {"type": "number"},
                "page": {"type": "number"},
                "per_page": {"type": "number"},
                "all": {"type": "boolean", "description": "barcha sahifalarni o'qish"},
            }},
            list_projects,
        ))

        async def get_project(p: dict) -> str:
            if "project_id" not in p:
                return self._err("project_id is required")
            try:
                return self._ok(await c.get(f"/project/{int(p['project_id'])}"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_get_project",
            "TopKey: Loyiha tafsilotlari (project_id bo'yicha).",
            {"type": "object", "required": ["project_id"], "properties": {
                "project_id": {"type": "number"},
            }},
            get_project,
        ))

        async def list_project_members(p: dict) -> str:
            if "project_id" not in p:
                return self._err("project_id is required")
            try:
                return self._ok(await c.get(f"/project/{int(p['project_id'])}/members"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_list_project_members",
            "TopKey: Loyihaga biriktirilgan xodimlar.",
            {"type": "object", "required": ["project_id"], "properties": {
                "project_id": {"type": "number"},
            }},
            list_project_members,
        ))

        # ── WORK / TASKS (5) ─────────────────────────────────

        async def list_tasks(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if "project_id" in p:
                    params["project_id"] = p["project_id"]
                if "assigned_to" in p:
                    params["assigned_to"] = p["assigned_to"]
                if "status" in p:
                    params["status"] = p["status"]
                if "board_column_id" in p:
                    params["board_column_id"] = p["board_column_id"]
                if "page" in p:
                    params["page"] = p["page"]
                if "per_page" in p:
                    params["per_page"] = p["per_page"]
                return self._ok(await c.get("/task", params or None))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_list_tasks",
            "TopKey: Vazifalar ro'yxati. Filter: project_id, assigned_to (user_id), status, board_column_id.",
            {"type": "object", "properties": {
                "project_id": {"type": "number"},
                "assigned_to": {"type": "number"},
                "status": {"type": "string"},
                "board_column_id": {"type": "number"},
                "page": {"type": "number"},
                "per_page": {"type": "number"},
            }},
            list_tasks,
        ))

        async def get_task(p: dict) -> str:
            if "task_id" not in p:
                return self._err("task_id is required")
            try:
                return self._ok(await c.get(f"/task/{int(p['task_id'])}"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_get_task",
            "TopKey: Vazifa to'liq ma'lumotlari (task_id bo'yicha).",
            {"type": "object", "required": ["task_id"], "properties": {
                "task_id": {"type": "number"},
            }},
            get_task,
        ))

        async def create_task(p: dict) -> str:
            for k in ("title", "project_id"):
                if k not in p:
                    return self._err(f"{k} is required")
            try:
                body: dict[str, Any] = {
                    "heading": p["title"],  # TopKey field name is `heading`
                    "title": p["title"],    # also accepted by some validators
                    "project_id": int(p["project_id"]),
                }
                if "description" in p:
                    body["description"] = p["description"]
                if "assigned_to" in p:
                    body["user_id"] = int(p["assigned_to"])
                if "due_date" in p:
                    body["due_date"] = p["due_date"]
                if "priority" in p:
                    body["priority"] = p["priority"]
                return self._ok(await c.post("/task", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_create_task",
            "TopKey: Yangi vazifa yaratish (title, project_id majburiy; assigned_to, due_date, priority ixtiyoriy).",
            {"type": "object", "required": ["title", "project_id"], "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "project_id": {"type": "number"},
                "assigned_to": {"type": "number", "description": "user_id"},
                "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                "priority": {"type": "string", "description": "low | medium | high"},
            }},
            create_task,
        ))

        async def update_task_status(p: dict) -> str:
            if "task_id" not in p:
                return self._err("task_id is required")
            if "status" not in p and "board_column_id" not in p:
                return self._err("either status or board_column_id is required")
            try:
                body: dict[str, Any] = {}
                if "status" in p:
                    body["status"] = p["status"]
                if "board_column_id" in p:
                    body["board_column_id"] = int(p["board_column_id"])
                return self._ok(await c.put(f"/task/{int(p['task_id'])}", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_update_task_status",
            "TopKey: Vazifa statusini o'zgartirish (status YOKI board_column_id orqali).",
            {"type": "object", "required": ["task_id"], "properties": {
                "task_id": {"type": "number"},
                "status": {"type": "string"},
                "board_column_id": {"type": "number"},
            }},
            update_task_status,
        ))

        async def list_subtasks(p: dict) -> str:
            if "task_id" not in p:
                return self._err("task_id is required")
            try:
                return self._ok(await c.get(f"/task/{int(p['task_id'])}/subtask"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_list_subtasks",
            "TopKey: Vazifa ostidagi sub-vazifalar.",
            {"type": "object", "required": ["task_id"], "properties": {
                "task_id": {"type": "number"},
            }},
            list_subtasks,
        ))

        # ── WORK / TIME (2) ──────────────────────────────────

        async def log_time(p: dict) -> str:
            for k in ("task_id", "hours"):
                if k not in p:
                    return self._err(f"{k} is required")
            try:
                body: dict[str, Any] = {
                    "task_id": int(p["task_id"]),
                    "total_hours": float(p["hours"]),
                }
                if "date" in p:
                    body["date"] = p["date"]
                if "memo" in p:
                    body["memo"] = p["memo"]
                return self._ok(await c.post("/timelog", body))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_log_time",
            "TopKey: Vazifaga sarflangan vaqtni qayd qilish (task_id, hours; ixtiyoriy date, memo).",
            {"type": "object", "required": ["task_id", "hours"], "properties": {
                "task_id": {"type": "number"},
                "hours": {"type": "number"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "memo": {"type": "string"},
            }},
            log_time,
        ))

        async def list_my_timelogs(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if "from_date" in p:
                    params["from_date"] = p["from_date"]
                if "to_date" in p:
                    params["to_date"] = p["to_date"]
                if "page" in p:
                    params["page"] = p["page"]
                if "per_page" in p:
                    params["per_page"] = p["per_page"]
                return self._ok(await c.get("/timelog/me", params or None))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_list_my_timelogs",
            "TopKey: Joriy foydalanuvchining vaqt qaydlari (from_date / to_date oralig'ida).",
            {"type": "object", "properties": {
                "from_date": {"type": "string"},
                "to_date": {"type": "string"},
                "page": {"type": "number"},
                "per_page": {"type": "number"},
            }},
            list_my_timelogs,
        ))

        # ── AUTH (3) ─────────────────────────────────────────

        async def login_tool(_p: dict) -> str:
            try:
                await c.login()
                return self._ok({"ok": True, "message": "Re-logged in successfully"})
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_login",
            "TopKey: Token qayta olish (majburiy login). Odatda kerak emas — 401 bo'lganda avto re-login ishlaydi.",
            {"type": "object", "properties": {}},
            login_tool,
        ))

        async def get_current_user(_p: dict) -> str:
            try:
                return self._ok(await c.get("/auth/me"))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_get_current_user",
            "TopKey: Hozirda qaysi foydalanuvchi (admin) sifatida bot ishlayotgani.",
            {"type": "object", "properties": {}},
            get_current_user,
        ))

        async def list_users(p: dict) -> str:
            try:
                params: dict[str, Any] = {}
                if "page" in p:
                    params["page"] = p["page"]
                if "per_page" in p:
                    params["per_page"] = p["per_page"]
                return self._ok(await c.get("/user", params or None))
            except Exception as e:
                return self._err(str(e))
        tools.append(ToolDef(
            "topkey_list_users",
            "TopKey: Tizim foydalanuvchilari ro'yxati (admin only).",
            {"type": "object", "properties": {
                "page": {"type": "number"},
                "per_page": {"type": "number"},
            }},
            list_users,
        ))

        return tools
