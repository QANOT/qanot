"""Google Sheets plugin for Qanot AI.

Tools on top of Google Sheets v4 + Drive v3, scoped with `drive.file`.
That scope is non-sensitive: the app can only touch spreadsheets the
user explicitly picks via Google Picker OR spreadsheets this app itself
creates. Everything else is walled off at the Google side — a real
security boundary, not just a convention.

The OAuth connect flow lives in qanotcloud (plane.topkey.uz/connect/google/…),
not in this plugin. After a successful connect, qanotcloud writes into the
bot's config.json:

    plugins: [
        {"name": "sheets", "config": {
            "refresh_token": "…",
            "client_id": "…",
            "client_secret": "…",
            "sheet_ids": ["abc…", "xyz…"],
            "sheet_names": ["Sotuvlar 2026", "Mijozlar"],
            "default_sheet_id": "abc…"
        }},
        …
    ]

The plugin takes it from there: refreshes access tokens on demand,
resolves sheet names → IDs, and exposes a small set of agent-friendly tools.

v0.1 scope:
  - sheets_health, sheets_list_connected
  - sheets_create (app auto-gains access to self-created files)
  - sheets_list_tabs, sheets_read, sheets_append, sheets_update
  - sheets_search (client-side scan; Sheets API has no text search)
  - sheets_share (Drive permission)
  - sheets_disconnect (in-memory only; persist requires platform-side edit)
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from qanot.plugins.base import Plugin, ToolDef, tool

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent

# Loader strips one entry after setup, so we insert twice to survive the
# later `from sh_engine.X import Y` imports. Mirrors notion/clipper.
sys.path.insert(0, str(PLUGIN_DIR))


class SheetsPlugin(Plugin):
    """Google Sheets integration (drive.file scope)."""

    name = "sheets"
    description = "Read and write the owner's Google Sheets"
    version = "0.1.0"

    def __init__(self) -> None:
        self._tokens: Any | None = None
        self._client: Any | None = None
        self._connected_sheets: list[dict[str, str]] = []
        self._default_sheet_id: str | None = None

    async def setup(self, config: dict) -> None:
        from qanot.secrets import resolve_secret

        cfg = config or {}
        try:
            refresh_token = _resolve(cfg.get("refresh_token"))
            client_id = _resolve(cfg.get("client_id"))
            client_secret = _resolve(cfg.get("client_secret"))
        except Exception as e:
            logger.error("Sheets secret resolution failed: %s", e)
            return

        if not (refresh_token and client_id and client_secret):
            logger.warning(
                "Sheets plugin loaded WITHOUT OAuth credentials — tools will "
                "return configuration errors until the user runs the connect flow."
            )
            return

        sheet_ids = list(cfg.get("sheet_ids") or [])
        sheet_names = list(cfg.get("sheet_names") or [])
        self._connected_sheets = [
            {
                "id": sid,
                "name": (
                    sheet_names[i] if i < len(sheet_names) and sheet_names[i] else sid
                ),
            }
            for i, sid in enumerate(sheet_ids)
        ]
        self._default_sheet_id = (
            cfg.get("default_sheet_id")
            or (sheet_ids[0] if sheet_ids else None)
        )

        try:
            from sh_engine.auth import TokenManager
            from sh_engine.client import SheetsClient

            self._tokens = TokenManager(refresh_token, client_id, client_secret)
            self._client = SheetsClient(self._tokens)
            logger.info(
                "Sheets plugin ready — %d sheet(s) connected, default=%s",
                len(self._connected_sheets),
                self._default_sheet_id,
            )
        except Exception as e:
            logger.error("Sheets plugin init failed: %s", e)

    async def teardown(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception as e:
                logger.debug("Sheets client close failed (non-fatal): %s", e)

    def get_tools(self) -> list[ToolDef]:
        return self._collect_tools()

    # ── Helpers ───────────────────────────────────────────────────

    def _not_configured(self) -> str:
        return json.dumps(
            {
                "status": "unconfigured",
                "error": (
                    "Google Sheets ulanmagan. Menyu → Integratsiyalar → "
                    "Google Sheets tugmasi orqali ulang."
                ),
            },
            ensure_ascii=False,
        )

    def _resolve_sheet_id(self, user_value: str | None) -> str | None:
        """Resolve the caller's spreadsheet identifier to a real ID.

        Priority:
          1. None/empty → default sheet ID (if any)
          2. Exact match against a connected sheet ID → that ID
          3. Case-insensitive match against a connected sheet NAME → its ID
          4. Otherwise → pass the raw value through (likely a spreadsheetId
             pasted by the user; Google will 404 if it's not accessible,
             which our error mapping surfaces as a friendly message)
        """
        if not user_value:
            return self._default_sheet_id
        value = user_value.strip()
        if not value:
            return self._default_sheet_id

        ids = {s["id"] for s in self._connected_sheets}
        if value in ids:
            return value

        low = value.lower()
        for s in self._connected_sheets:
            if s["name"].lower() == low:
                return s["id"]
        return value

    # ── Tools ─────────────────────────────────────────────────────

    @tool(
        name="sheets_health",
        description=(
            "Check Google Sheets connection: OAuth token refresh works and "
            "returns the list of connected spreadsheets. Call this FIRST "
            "when any other sheets_* tool is failing."
        ),
        parameters={"type": "object", "properties": {}},
    )
    async def sheets_health(self, params: dict) -> str:
        from sh_engine.errors import map_exception

        if self._client is None or self._tokens is None:
            return json.dumps(
                {
                    "status": "unconfigured",
                    "error": (
                        "Google Sheets OAuth ulanmagan. Menyu → "
                        "Integratsiyalar → Google Sheets → ulash."
                    ),
                    "connected_sheets": [],
                },
                ensure_ascii=False,
            )

        try:
            await self._tokens.get_access_token()
        except Exception as e:
            return json.dumps(
                {"status": "token_error", **map_exception(e)},
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "status": "ok",
                "connected_sheets": self._connected_sheets,
                "default_sheet_id": self._default_sheet_id,
                "hint": (
                    "Yangi sheet qo'shish uchun Menyu → Integratsiyalar → "
                    "Google Sheets → ulash. Agent o'zi yaratgan sheet-lar "
                    "avtomatik ishlaydi."
                ),
            },
            ensure_ascii=False,
        )

    @tool(
        name="sheets_list_connected",
        description=(
            "List spreadsheets connected to this bot with their IDs, names, "
            "and which one is the default. Use this to discover IDs before "
            "calling sheets_read/append/update."
        ),
        parameters={"type": "object", "properties": {}},
    )
    async def sheets_list_connected(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        return json.dumps(
            {
                "status": "ok",
                "count": len(self._connected_sheets),
                "default_sheet_id": self._default_sheet_id,
                "sheets": [
                    {**s, "is_default": s["id"] == self._default_sheet_id}
                    for s in self._connected_sheets
                ],
            },
            ensure_ascii=False,
        )

    @tool(
        name="sheets_create",
        description=(
            "Create a new Google Sheets spreadsheet in the owner's Drive. The "
            "agent automatically gains access to anything it creates (drive.file "
            "scope). Optional headers[] writes column names in row 1. Use this "
            "for 'bugungi savdolar', 'yangi mijozlar ro'yxati' style requests. "
            "Returns spreadsheet_id + URL."
        ),
        parameters={
            "type": "object",
            "required": ["title"],
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Spreadsheet title, e.g. 'Savdolar 2026-04-21'",
                },
                "tab_name": {
                    "type": "string",
                    "description": "First tab's name (default: 'Sheet1')",
                },
                "headers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Column headers to write in row 1",
                },
            },
        },
    )
    async def sheets_create(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        title = (params.get("title") or "").strip()
        if not title:
            return json.dumps({"error": "title is required"}, ensure_ascii=False)

        from sh_engine.errors import map_exception

        try:
            created = await self._client.create_spreadsheet(
                title=title,
                tab_name=params.get("tab_name"),
                headers=params.get("headers") or None,
            )
            sid = created.get("spreadsheetId")
            url = created.get("spreadsheetUrl")

            # Track the new sheet in-memory so follow-up tools work without a
            # reconnect. Persistence across restarts depends on the platform
            # writing this back to config.json (separate concern).
            if sid and not any(s["id"] == sid for s in self._connected_sheets):
                self._connected_sheets.append({"id": sid, "name": title})
                if self._default_sheet_id is None:
                    self._default_sheet_id = sid

            return json.dumps(
                {
                    "status": "ok",
                    "spreadsheet_id": sid,
                    "url": url,
                    "title": title,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("sheets_create failed")
            return json.dumps(
                {"status": "error", **map_exception(e)},
                ensure_ascii=False,
            )

    @tool(
        name="sheets_list_tabs",
        description=(
            "List tab (sheet) names inside a spreadsheet. Defaults to the "
            "current default spreadsheet. Use before sheets_read/append when "
            "you need to know tab names."
        ),
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": (
                        "Spreadsheet ID or its connected name. Optional — "
                        "defaults to the default spreadsheet."
                    ),
                },
            },
        },
    )
    async def sheets_list_tabs(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        sid = self._resolve_sheet_id(params.get("spreadsheet_id"))
        if not sid:
            return json.dumps(
                {"error": "No spreadsheet provided and no default connected"},
                ensure_ascii=False,
            )

        from sh_engine.errors import map_exception

        try:
            data = await self._client.get_spreadsheet(sid)
            tabs = []
            for s in data.get("sheets", []):
                p = s.get("properties", {})
                grid = p.get("gridProperties", {})
                tabs.append(
                    {
                        "name": p.get("title"),
                        "sheet_id": p.get("sheetId"),
                        "rows": grid.get("rowCount"),
                        "cols": grid.get("columnCount"),
                    }
                )
            return json.dumps(
                {
                    "status": "ok",
                    "spreadsheet_id": sid,
                    "title": data.get("properties", {}).get("title"),
                    "tabs": tabs,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("sheets_list_tabs failed")
            return json.dumps(
                {"status": "error", **map_exception(e)},
                ensure_ascii=False,
            )

    @tool(
        name="sheets_read",
        description=(
            "Read a range from a spreadsheet. Range uses A1 notation. "
            "Examples: 'Savdolar!A1:C10' (specific cells), 'Savdolar!A:C' "
            "(whole columns), 'Savdolar' (whole tab). Empty cells come back "
            "as missing trailing entries, not nulls."
        ),
        parameters={
            "type": "object",
            "required": ["range"],
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Spreadsheet ID or connected name (optional, defaults to default)",
                },
                "range": {
                    "type": "string",
                    "description": "A1 range, e.g. 'Savdolar!A1:C10'",
                },
            },
        },
    )
    async def sheets_read(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        sid = self._resolve_sheet_id(params.get("spreadsheet_id"))
        range_ = (params.get("range") or "").strip()
        if not sid or not range_:
            return json.dumps(
                {"error": "spreadsheet_id (or default) and range are required"},
                ensure_ascii=False,
            )

        from sh_engine.errors import map_exception

        try:
            values = await self._client.read_values(sid, range_)
            return json.dumps(
                {
                    "status": "ok",
                    "spreadsheet_id": sid,
                    "range": range_,
                    "row_count": len(values),
                    "values": values,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("sheets_read failed")
            return json.dumps(
                {"status": "error", **map_exception(e)},
                ensure_ascii=False,
            )

    @tool(
        name="sheets_append",
        description=(
            "Append one or more rows to the END of a tab. Each inner array is "
            "one row. Values are written with USER_ENTERED (formulas evaluated, "
            "dates auto-parsed). Use this for logging new events — sales, "
            "clients, expenses."
        ),
        parameters={
            "type": "object",
            "required": ["range", "values"],
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Spreadsheet ID (optional, defaults to default)",
                },
                "range": {
                    "type": "string",
                    "description": (
                        "Tab name or A1 range where to append. Simplest: tab "
                        "name alone ('Savdolar'). For more control: 'Savdolar!A1'."
                    ),
                },
                "values": {
                    "type": "array",
                    "items": {"type": "array"},
                    "description": (
                        "2D array of rows. e.g. "
                        "[['2026-04-21', 'Akmal', 150000, 'naqd']]"
                    ),
                },
            },
        },
    )
    async def sheets_append(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        sid = self._resolve_sheet_id(params.get("spreadsheet_id"))
        range_ = (params.get("range") or "").strip()
        values = params.get("values") or []
        if not sid or not range_:
            return json.dumps(
                {"error": "spreadsheet_id (or default) and range are required"},
                ensure_ascii=False,
            )
        if not isinstance(values, list) or not values:
            return json.dumps(
                {"error": "values must be a non-empty 2D array"},
                ensure_ascii=False,
            )
        if not all(isinstance(r, list) for r in values):
            return json.dumps(
                {"error": "values must be an array of arrays (rows of cells)"},
                ensure_ascii=False,
            )

        from sh_engine.errors import map_exception

        try:
            result = await self._client.append_values(sid, range_, values)
            updates = result.get("updates", {}) or {}
            return json.dumps(
                {
                    "status": "ok",
                    "spreadsheet_id": sid,
                    "updated_range": updates.get("updatedRange"),
                    "updated_rows": updates.get("updatedRows"),
                    "updated_cells": updates.get("updatedCells"),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("sheets_append failed")
            return json.dumps(
                {"status": "error", **map_exception(e)},
                ensure_ascii=False,
            )

    @tool(
        name="sheets_update",
        description=(
            "Overwrite cells in an explicit range. Unlike sheets_append this "
            "WRITES OVER existing cells. Use for corrections or when you know "
            "the exact row/column to change."
        ),
        parameters={
            "type": "object",
            "required": ["range", "values"],
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {
                    "type": "string",
                    "description": "A1 range to overwrite, e.g. 'Savdolar!B5:D5'",
                },
                "values": {
                    "type": "array",
                    "items": {"type": "array"},
                    "description": "2D array of new values sized to match the range",
                },
            },
        },
    )
    async def sheets_update(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        sid = self._resolve_sheet_id(params.get("spreadsheet_id"))
        range_ = (params.get("range") or "").strip()
        values = params.get("values") or []
        if not sid or not range_:
            return json.dumps(
                {"error": "spreadsheet_id (or default) and range are required"},
                ensure_ascii=False,
            )
        if not isinstance(values, list) or not values:
            return json.dumps(
                {"error": "values must be a non-empty 2D array"},
                ensure_ascii=False,
            )
        if not all(isinstance(r, list) for r in values):
            return json.dumps(
                {"error": "values must be an array of arrays"},
                ensure_ascii=False,
            )

        from sh_engine.errors import map_exception

        try:
            result = await self._client.update_values(sid, range_, values)
            return json.dumps(
                {
                    "status": "ok",
                    "spreadsheet_id": sid,
                    "updated_range": result.get("updatedRange"),
                    "updated_rows": result.get("updatedRows"),
                    "updated_cells": result.get("updatedCells"),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("sheets_update failed")
            return json.dumps(
                {"status": "error", **map_exception(e)},
                ensure_ascii=False,
            )

    @tool(
        name="sheets_search",
        description=(
            "Find rows in a tab where ANY cell contains the query "
            "(case-insensitive substring). Returns rows as dicts keyed by the "
            "header row. Good for 'mijoz Akmal' style lookups. Full-tab scan — "
            "not suitable for 100k+ row sheets."
        ),
        parameters={
            "type": "object",
            "required": ["tab", "query"],
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "tab": {
                    "type": "string",
                    "description": "Tab name, e.g. 'Mijozlar'",
                },
                "query": {
                    "type": "string",
                    "description": "Substring to look for (case-insensitive)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (1-200, default 50)",
                },
            },
        },
    )
    async def sheets_search(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        sid = self._resolve_sheet_id(params.get("spreadsheet_id"))
        tab = (params.get("tab") or "").strip()
        query = params.get("query") or ""
        try:
            limit = int(params.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(200, limit))

        if not sid or not tab or not query:
            return json.dumps(
                {"error": "spreadsheet_id (or default), tab, and query are required"},
                ensure_ascii=False,
            )

        from sh_engine.errors import map_exception

        try:
            matches = await self._client.search_values(sid, tab, query, limit=limit)
            return json.dumps(
                {
                    "status": "ok",
                    "spreadsheet_id": sid,
                    "tab": tab,
                    "query": query,
                    "count": len(matches),
                    "rows": matches,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("sheets_search failed")
            return json.dumps(
                {"status": "error", **map_exception(e)},
                ensure_ascii=False,
            )

    @tool(
        name="sheets_share",
        description=(
            "Share a spreadsheet with someone by email. Role 'writer' = edit, "
            "'reader' = read-only. Google sends an invite email by default. "
            "Useful for sharing agent-created reports with an accountant or "
            "team member."
        ),
        parameters={
            "type": "object",
            "required": ["email"],
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "email": {
                    "type": "string",
                    "description": "Recipient Google email",
                },
                "role": {
                    "type": "string",
                    "enum": ["writer", "reader"],
                    "description": "Permission level (default: writer)",
                },
            },
        },
    )
    async def sheets_share(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        sid = self._resolve_sheet_id(params.get("spreadsheet_id"))
        email = (params.get("email") or "").strip()
        role = (params.get("role") or "writer").strip()
        if not sid or not email:
            return json.dumps(
                {"error": "spreadsheet_id (or default) and email are required"},
                ensure_ascii=False,
            )
        if role not in ("writer", "reader"):
            return json.dumps(
                {"error": "role must be 'writer' or 'reader'"},
                ensure_ascii=False,
            )

        from sh_engine.errors import map_exception

        try:
            result = await self._client.share(sid, email, role=role)
            return json.dumps(
                {
                    "status": "ok",
                    "spreadsheet_id": sid,
                    "permission_id": result.get("id"),
                    "email": email,
                    "role": role,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("sheets_share failed")
            return json.dumps(
                {"status": "error", **map_exception(e)},
                ensure_ascii=False,
            )

    @tool(
        name="sheets_disconnect",
        description=(
            "Remove a spreadsheet from this bot's connected list (in-memory, "
            "for the current session). NOTE: to fully revoke Google access, "
            "open Google Account → Security → Third-party apps and remove "
            "Qanot there. To persist the removal in config.json, run the "
            "connect flow again and deselect this sheet."
        ),
        parameters={
            "type": "object",
            "required": ["spreadsheet_id"],
            "properties": {
                "spreadsheet_id": {"type": "string"},
            },
        },
    )
    async def sheets_disconnect(self, params: dict) -> str:
        if self._client is None:
            return self._not_configured()
        sid = self._resolve_sheet_id(params.get("spreadsheet_id"))
        if not sid:
            return json.dumps(
                {"error": "spreadsheet_id is required"},
                ensure_ascii=False,
            )

        before = len(self._connected_sheets)
        self._connected_sheets = [
            s for s in self._connected_sheets if s["id"] != sid
        ]
        removed = before > len(self._connected_sheets)
        if self._default_sheet_id == sid:
            self._default_sheet_id = (
                self._connected_sheets[0]["id"]
                if self._connected_sheets
                else None
            )
        return json.dumps(
            {
                "status": "ok" if removed else "not_found",
                "spreadsheet_id": sid,
                "remaining_connected": len(self._connected_sheets),
                "hint": (
                    "In-memory state updated. Config.json still holds the old "
                    "list — qayta ulanishda ro'yxat tiklandi bo'ladi."
                ),
            },
            ensure_ascii=False,
        )


# ── Module helpers ───────────────────────────────────────────────

def _resolve(raw: Any) -> str | None:
    """Resolve a qanot SecretRef (dict like {'env': 'X'}) or plain string.

    We tolerate the resolver import failing or raising because plugin configs
    in Qanot Cloud tend to be plain strings written by the connect flow.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw or None
    try:
        from qanot.secrets import resolve_secret

        value = resolve_secret(raw)
        return value or None
    except Exception as e:
        logger.warning("Sheets plugin: secret resolve failed, using raw: %s", e)
        if isinstance(raw, str):
            return raw or None
        return None
