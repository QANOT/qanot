"""Notion Plugin — workspace integration for Qanot AI.

Exposes a curated, agent-friendly tool surface over the Notion API
(not raw REST) so the LLM can search, read, create, and update pages
and database entries in the owner's Notion workspace.

v0.1 scope:
  - notion_health: token + accessible-resources check

Subsequent iterations add: search, read_page, append_to_page,
create_page, query_database, update_page_properties,
get_database_schema.

Design notes:
  - Single-tenant v1: one NOTION_API_KEY integration token per bot.
    Multi-tenant OAuth flow is deferred to QanotCloud.
  - Responses are always JSON strings (plugin convention). On error,
    `{"error": "...", "code": "...", "status": ...}` via `engine.errors`.
  - Client is created at setup() time and reused — notion-client handles
    retries/backoff internally.
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

# Match clipper's sys.path trick: loader inserts plugin dir and later does
# list.remove() which drops only the first occurrence. Insert twice so at
# least one copy survives for later `from engine.X import Y` calls.
sys.path.insert(0, str(PLUGIN_DIR))


class NotionPlugin(Plugin):
    """Notion workspace integration."""

    name = "notion"
    description = "Search and edit the owner's Notion workspace"
    version = "0.1.0"

    def __init__(self) -> None:
        self._token: str | None = None
        self._client: Any | None = None

    async def setup(self, config: dict) -> None:
        # `api_key` is declared in plugin.json required_config so the loader
        # already warned if it's missing. We still tolerate absence — tools
        # fail with a clear message rather than refusing to load.
        #
        # qanot's SecretRef resolver only unwraps a hard-coded set of
        # TOP-LEVEL config fields (api_key, bot_token, brave_api_key, …).
        # Plugin configs are passed through as-is, so if the user writes
        # {"api_key": {"env": "NOTION_API_KEY"}} in config.json we receive
        # the literal dict. Resolve it here.
        from qanot.secrets import resolve_secret
        raw = (config or {}).get("api_key")
        try:
            self._token = resolve_secret(raw) if raw is not None else None
        except Exception as e:
            logger.error("Notion api_key resolution failed: %s", e)
            self._token = None
        if not self._token:
            logger.warning(
                "Notion plugin loaded WITHOUT api_key — tools will return "
                "configuration errors until NOTION_API_KEY is set."
            )
            return

        try:
            from engine.client import make_client
            self._client = make_client(self._token)
            logger.info("Notion plugin ready (token configured)")
        except Exception as e:
            logger.error("Notion plugin init failed: %s", e)
            self._client = None

    async def teardown(self) -> None:
        # notion-client AsyncClient holds an httpx.AsyncClient; close it.
        client = self._client
        if client is not None:
            try:
                await client.aclose()
            except Exception as e:
                logger.debug("Notion client close failed (non-fatal): %s", e)
        self._client = None

    def get_tools(self) -> list[ToolDef]:
        return self._collect_tools()

    # ── Tools ─────────────────────────────────────────────────────

    @tool(
        name="notion_health",
        description=(
            "Check whether the Notion integration is configured and reachable. "
            "Returns token status plus a small sample of accessible pages/databases. "
            "Call this first when debugging why a Notion tool is failing."
        ),
        parameters={"type": "object", "properties": {}},
    )
    async def notion_health(self, params: dict) -> str:
        from engine.errors import map_exception

        if not self._token:
            return json.dumps({
                "status": "unconfigured",
                "error": (
                    "NOTION_API_KEY is not set. Create an integration at "
                    "https://notion.so/my-integrations → copy the Internal "
                    "Integration secret → set it as NOTION_API_KEY env var."
                ),
            }, ensure_ascii=False)

        if self._client is None:
            return json.dumps({
                "status": "init_failed",
                "error": "Notion client failed to initialise — see container logs.",
            }, ensure_ascii=False)

        try:
            # `search` with empty query + small page_size is the cheapest call
            # that also confirms the token has at least read access to *something*.
            resp = await self._client.search(query="", page_size=5)
            samples = []
            for item in (resp or {}).get("results", [])[:5]:
                obj_type = item.get("object")
                title = ""
                # Title lives in different spots depending on object type.
                props = item.get("properties") or {}
                for key in ("title", "Name", "name"):
                    v = props.get(key)
                    if v and v.get("title"):
                        title = "".join(
                            (p.get("plain_text") or "") for p in v["title"]
                        )
                        break
                if not title:
                    # Pages returned by search sometimes have top-level "title"
                    top = item.get("title") or []
                    if isinstance(top, list):
                        title = "".join(
                            (p.get("plain_text") or "") for p in top
                        )
                samples.append({
                    "object": obj_type,
                    "id": item.get("id"),
                    "title": title or "(untitled)",
                    "url": item.get("url"),
                })
            return json.dumps({
                "status": "ok",
                "accessible_sample": samples,
                "sample_count": len(samples),
                "hint": (
                    "If sample_count is 0, the integration has no shared pages yet. "
                    "Open a Notion page → … menu → Add connections → select the integration."
                ),
            }, ensure_ascii=False)
        except Exception as e:
            logger.exception("notion_health failed")
            return json.dumps({
                "status": "error",
                **map_exception(e),
            }, ensure_ascii=False)

    # ── Helpers ───────────────────────────────────────────────────

    def _not_configured(self) -> str:
        return json.dumps({
            "status": "unconfigured",
            "error": "Notion token not configured. Run notion_health for setup instructions.",
        }, ensure_ascii=False)

    @staticmethod
    def _extract_title(obj: dict) -> str:
        """Best-effort page/database title extraction across Notion response shapes."""
        if not isinstance(obj, dict):
            return ""
        # Databases return title at the top level.
        top = obj.get("title")
        if isinstance(top, list) and top:
            return "".join(t.get("plain_text", "") for t in top)
        # Pages store title inside a property typically named "title" or "Name".
        props = obj.get("properties") or {}
        for key in ("title", "Name", "name"):
            v = props.get(key)
            if isinstance(v, dict) and v.get("title"):
                return "".join(t.get("plain_text", "") for t in v["title"])
        # Scan all properties for the first one with type=title.
        for v in props.values():
            if isinstance(v, dict) and v.get("type") == "title":
                title_rt = v.get("title") or []
                return "".join(t.get("plain_text", "") for t in title_rt)
        return ""

    @staticmethod
    def _summarise_properties(props: dict) -> dict:
        """Flatten DB row properties into a compact {name: readable_value} dict."""
        out: dict[str, Any] = {}
        for name, v in (props or {}).items():
            if not isinstance(v, dict):
                continue
            t = v.get("type")
            val: Any = None
            if t == "title":
                val = "".join(r.get("plain_text", "") for r in v.get("title") or [])
            elif t == "rich_text":
                val = "".join(r.get("plain_text", "") for r in v.get("rich_text") or [])
            elif t == "number":
                val = v.get("number")
            elif t == "select":
                sel = v.get("select") or {}
                val = sel.get("name")
            elif t == "multi_select":
                val = [s.get("name") for s in (v.get("multi_select") or [])]
            elif t == "status":
                st = v.get("status") or {}
                val = st.get("name")
            elif t == "checkbox":
                val = v.get("checkbox")
            elif t == "url":
                val = v.get("url")
            elif t == "email":
                val = v.get("email")
            elif t == "phone_number":
                val = v.get("phone_number")
            elif t == "date":
                d = v.get("date") or {}
                val = {"start": d.get("start"), "end": d.get("end")}
            elif t == "people":
                val = [p.get("name") or p.get("id") for p in (v.get("people") or [])]
            elif t == "files":
                val = [f.get("name") for f in (v.get("files") or [])]
            elif t == "relation":
                val = [r.get("id") for r in (v.get("relation") or [])]
            elif t == "formula":
                f = v.get("formula") or {}
                val = f.get(f.get("type"))
            elif t == "rollup":
                r = v.get("rollup") or {}
                val = r.get(r.get("type"))
            elif t == "created_time":
                val = v.get("created_time")
            elif t == "last_edited_time":
                val = v.get("last_edited_time")
            elif t == "created_by":
                val = (v.get("created_by") or {}).get("id")
            elif t == "last_edited_by":
                val = (v.get("last_edited_by") or {}).get("id")
            out[name] = val
        return out

    async def _resolve_data_source_id(self, database_id: str) -> str:
        """Notion API 2025-09-03: a database holds N data sources, and queries
        target the data source (not the database). We resolve database_id to
        its first data source id so LLM-facing tools can still accept the
        familiar `database_id` parameter.
        """
        db = await self._client.databases.retrieve(database_id=database_id)
        sources = db.get("data_sources") or []
        if not sources:
            raise RuntimeError(
                "Database has no data sources — schema may be broken or the API "
                "did not return data_sources[]. Try sharing the DB with the integration again."
            )
        return sources[0]["id"]

    async def _fetch_all_children(self, block_id: str, max_blocks: int = 500) -> list[dict]:
        """Paginate through a block's direct children. Does NOT recurse."""
        from engine.client import iterate_paginated
        collected: list[dict] = []

        async def fetch_page(**kwargs):
            return await self._client.blocks.children.list(
                block_id=block_id, **kwargs,
            )

        async for item in iterate_paginated(
            fetch_page, page_size=100, max_items=max_blocks,
        ):
            collected.append(item)
        return collected

    # ── Search ────────────────────────────────────────────────────

    @tool(
        name="notion_search",
        description=(
            "Search the connected Notion workspace for pages and/or databases by title. "
            "Empty query returns the most recently edited items. Use this to discover "
            "page/database IDs before reading or writing. Returns compact list with "
            "id, object type, title, url."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to search for in titles. Empty = browse recent.",
                },
                "object_type": {
                    "type": "string",
                    "enum": ["page", "database", "all"],
                    "description": "Filter to one object type. Default: all.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (1-50). Default: 10.",
                },
            },
        },
    )
    async def notion_search(self, params: dict) -> str:
        if not self._client:
            return self._not_configured()
        query = str(params.get("query") or "")
        obj_type = (params.get("object_type") or "all").strip().lower()
        limit = max(1, min(50, int(params.get("limit") or 10)))

        from engine.errors import map_exception
        kwargs: dict[str, Any] = {"query": query, "page_size": limit}
        if obj_type in ("page", "database", "data_source"):
            # Notion API 2025-09-03: "database" was renamed to "data_source"
            # for search filters. We let callers still say "database" for
            # ergonomics and translate here.
            api_val = "data_source" if obj_type == "database" else obj_type
            kwargs["filter"] = {"value": api_val, "property": "object"}

        try:
            resp = await self._client.search(**kwargs)
            items = []
            for r in (resp or {}).get("results", [])[:limit]:
                items.append({
                    "id": r.get("id"),
                    "object": r.get("object"),
                    "title": self._extract_title(r) or "(untitled)",
                    "url": r.get("url"),
                    "last_edited_time": r.get("last_edited_time"),
                })
            return json.dumps({
                "status": "ok",
                "count": len(items),
                "results": items,
            }, ensure_ascii=False)
        except Exception as e:
            logger.exception("notion_search failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    # ── Read page (content + properties as markdown) ──────────────

    @tool(
        name="notion_read_page",
        description=(
            "Read a Notion page's content and properties. Returns the page title, URL, "
            "all top-level blocks rendered as markdown, and a compact properties summary. "
            "Nested/child blocks are summarised but not recursed (use notion_search to find them)."
        ),
        parameters={
            "type": "object",
            "required": ["page_id"],
            "properties": {
                "page_id": {"type": "string", "description": "Notion page ID (UUID with or without dashes)"},
                "max_blocks": {"type": "integer", "description": "Max blocks to fetch (default 200)"},
            },
        },
    )
    async def notion_read_page(self, params: dict) -> str:
        if not self._client:
            return self._not_configured()
        page_id = (params.get("page_id") or "").strip()
        if not page_id:
            return json.dumps({"error": "page_id is required"}, ensure_ascii=False)
        max_blocks = max(1, min(1000, int(params.get("max_blocks") or 200)))

        from engine.errors import map_exception
        from engine.markdown import blocks_to_markdown

        try:
            page = await self._client.pages.retrieve(page_id=page_id)
            blocks = await self._fetch_all_children(page_id, max_blocks=max_blocks)
            md = blocks_to_markdown(blocks)
            return json.dumps({
                "status": "ok",
                "id": page.get("id"),
                "title": self._extract_title(page) or "(untitled)",
                "url": page.get("url"),
                "properties": self._summarise_properties(page.get("properties") or {}),
                "markdown": md,
                "block_count": len(blocks),
            }, ensure_ascii=False)
        except Exception as e:
            logger.exception("notion_read_page failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    # ── Append to page ────────────────────────────────────────────

    @tool(
        name="notion_append_to_page",
        description=(
            "Append markdown content to the END of an existing Notion page or block. "
            "Supports: # headings, - bullets, 1. numbered, - [ ] todos, > quotes, "
            "```code blocks```, ---dividers, and inline **bold**/*italic*/`code`/[links](url)."
        ),
        parameters={
            "type": "object",
            "required": ["page_id", "markdown"],
            "properties": {
                "page_id": {"type": "string", "description": "Target page (or block) ID"},
                "markdown": {"type": "string", "description": "Markdown content to append"},
            },
        },
    )
    async def notion_append_to_page(self, params: dict) -> str:
        if not self._client:
            return self._not_configured()
        page_id = (params.get("page_id") or "").strip()
        md = params.get("markdown") or ""
        if not page_id:
            return json.dumps({"error": "page_id is required"}, ensure_ascii=False)
        if not md.strip():
            return json.dumps({"error": "markdown is required (non-empty)"}, ensure_ascii=False)

        from engine.errors import map_exception
        from engine.markdown import markdown_to_blocks

        blocks = markdown_to_blocks(md)
        if not blocks:
            return json.dumps({"error": "markdown produced no blocks"}, ensure_ascii=False)

        try:
            # Notion append supports up to 100 blocks per request; chunk if needed.
            created = 0
            for i in range(0, len(blocks), 100):
                chunk = blocks[i:i + 100]
                await self._client.blocks.children.append(
                    block_id=page_id, children=chunk,
                )
                created += len(chunk)
            return json.dumps({
                "status": "ok",
                "appended_blocks": created,
            }, ensure_ascii=False)
        except Exception as e:
            logger.exception("notion_append_to_page failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    # ── Create page ───────────────────────────────────────────────

    @tool(
        name="notion_create_page",
        description=(
            "Create a new Notion page. Provide EITHER parent_page_id (child under a page) "
            "OR parent_database_id (new row in a database). For database rows, `properties` "
            "must match the DB schema — call notion_get_database_schema first to learn columns."
        ),
        parameters={
            "type": "object",
            "required": ["title"],
            "properties": {
                "parent_page_id": {"type": "string", "description": "Parent page UUID (mutually exclusive with parent_database_id)"},
                "parent_database_id": {"type": "string", "description": "Parent database UUID"},
                "title": {"type": "string", "description": "Page title (also used as the title property if parent is a DB)"},
                "markdown": {"type": "string", "description": "Optional initial body content as markdown"},
                "properties": {
                    "type": "object",
                    "description": (
                        "Extra DB properties as {name: value}. Supported simple types: "
                        "string → rich_text, number → number, bool → checkbox, "
                        "list[str] → multi_select, str → select (for known select/status). "
                        "For full control, pass raw Notion property objects."
                    ),
                },
            },
        },
    )
    async def notion_create_page(self, params: dict) -> str:
        if not self._client:
            return self._not_configured()
        parent_page_id = (params.get("parent_page_id") or "").strip()
        parent_db_id = (params.get("parent_database_id") or "").strip()
        title = (params.get("title") or "").strip()
        md = params.get("markdown") or ""
        props_in: dict = params.get("properties") or {}

        if not title:
            return json.dumps({"error": "title is required"}, ensure_ascii=False)
        if bool(parent_page_id) == bool(parent_db_id):
            return json.dumps({
                "error": "Exactly one of parent_page_id or parent_database_id is required",
            }, ensure_ascii=False)

        from engine.errors import map_exception
        from engine.markdown import markdown_to_blocks, markdown_to_rich_text

        # Parent — for DBs, API 2025-09-03 requires data_source_id, not database_id.
        properties: dict[str, Any] = {}
        title_property_name = "title"
        parent: dict[str, Any]
        if parent_page_id:
            parent = {"page_id": parent_page_id}
        else:
            try:
                db = await self._client.databases.retrieve(database_id=parent_db_id)
                data_sources = db.get("data_sources") or []
                if not data_sources:
                    return json.dumps({
                        "status": "error",
                        "error": "Database returned no data_sources — cannot create row.",
                    }, ensure_ascii=False)
                ds_id = data_sources[0]["id"]
                ds = await self._client.data_sources.retrieve(data_source_id=ds_id)
                for name, schema in (ds.get("properties") or {}).items():
                    if isinstance(schema, dict) and schema.get("type") == "title":
                        title_property_name = name
                        break
                parent = {"type": "data_source_id", "data_source_id": ds_id}
            except Exception as e:
                logger.exception("notion_create_page: data source lookup failed")
                return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

        properties[title_property_name] = {
            "title": markdown_to_rich_text(title) or [
                {"type": "text", "text": {"content": title, "link": None},
                 "annotations": {"bold": False, "italic": False, "strikethrough": False,
                                 "underline": False, "code": False, "color": "default"},
                 "plain_text": title}
            ],
        }

        # Translate simple extra properties.
        for name, value in props_in.items():
            if name == title_property_name:
                continue
            if isinstance(value, dict) and any(
                k in value for k in ("rich_text", "number", "checkbox", "select",
                                     "multi_select", "date", "url", "status")
            ):
                # Caller passed a raw Notion property object — trust it.
                properties[name] = value
            elif isinstance(value, bool):
                properties[name] = {"checkbox": value}
            elif isinstance(value, (int, float)):
                properties[name] = {"number": value}
            elif isinstance(value, list):
                properties[name] = {
                    "multi_select": [{"name": str(v)} for v in value],
                }
            elif isinstance(value, str):
                # Default to rich_text; callers who want select/status must pass raw objects.
                properties[name] = {"rich_text": markdown_to_rich_text(value)}
            elif value is None:
                continue
            else:
                properties[name] = {"rich_text": markdown_to_rich_text(str(value))}

        children = markdown_to_blocks(md) if md.strip() else []

        try:
            created = await self._client.pages.create(
                parent=parent,
                properties=properties,
                children=children[:100],  # first 100 blocks inline
            )
            # Append remaining blocks if body was large.
            remaining = children[100:]
            appended = 0
            while remaining:
                chunk = remaining[:100]
                await self._client.blocks.children.append(
                    block_id=created["id"], children=chunk,
                )
                appended += len(chunk)
                remaining = remaining[100:]

            return json.dumps({
                "status": "ok",
                "id": created.get("id"),
                "url": created.get("url"),
                "blocks_created": len(children[:100]) + appended,
                "title_property_name": title_property_name,
            }, ensure_ascii=False)
        except Exception as e:
            logger.exception("notion_create_page failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    # ── Query database ────────────────────────────────────────────

    @tool(
        name="notion_query_database",
        description=(
            "Query rows in a Notion database with optional filter and sorts. "
            "Returns a compact list of rows with id, title, url, and summarised properties. "
            "Call notion_get_database_schema first to learn property names for filters."
        ),
        parameters={
            "type": "object",
            "required": ["database_id"],
            "properties": {
                "database_id": {"type": "string"},
                "filter": {
                    "type": "object",
                    "description": "Raw Notion filter object (see Notion API docs). Example: {\"property\":\"Status\",\"select\":{\"equals\":\"Done\"}}",
                },
                "sorts": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Raw Notion sorts array. Example: [{\"property\":\"Date\",\"direction\":\"descending\"}]",
                },
                "limit": {"type": "integer", "description": "Max rows (1-100). Default 20."},
            },
        },
    )
    async def notion_query_database(self, params: dict) -> str:
        if not self._client:
            return self._not_configured()
        db_id = (params.get("database_id") or "").strip()
        if not db_id:
            return json.dumps({"error": "database_id is required"}, ensure_ascii=False)
        limit = max(1, min(100, int(params.get("limit") or 20)))

        from engine.errors import map_exception
        try:
            # Notion API 2025-09-03: query goes through data_sources, not
            # databases. Resolve the DB → its first data_source.
            data_source_id = await self._resolve_data_source_id(db_id)
            kwargs: dict[str, Any] = {
                "data_source_id": data_source_id,
                "page_size": limit,
            }
            if isinstance(params.get("filter"), dict):
                kwargs["filter"] = params["filter"]
            if isinstance(params.get("sorts"), list):
                kwargs["sorts"] = params["sorts"]
            resp = await self._client.data_sources.query(**kwargs)
            rows = []
            for r in (resp or {}).get("results", [])[:limit]:
                rows.append({
                    "id": r.get("id"),
                    "url": r.get("url"),
                    "title": self._extract_title(r) or "(untitled)",
                    "properties": self._summarise_properties(r.get("properties") or {}),
                    "last_edited_time": r.get("last_edited_time"),
                })
            return json.dumps({
                "status": "ok",
                "count": len(rows),
                "has_more": bool(resp.get("has_more")),
                "rows": rows,
            }, ensure_ascii=False)
        except Exception as e:
            logger.exception("notion_query_database failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    # ── Update page properties ────────────────────────────────────

    @tool(
        name="notion_update_page_properties",
        description=(
            "Update one or more properties on an existing page (including database rows). "
            "Values follow the same shortcut rules as notion_create_page: bool→checkbox, "
            "num→number, list→multi_select, str→rich_text. Pass raw Notion property objects for full control."
        ),
        parameters={
            "type": "object",
            "required": ["page_id", "properties"],
            "properties": {
                "page_id": {"type": "string"},
                "properties": {"type": "object", "description": "Map of property name → value"},
            },
        },
    )
    async def notion_update_page_properties(self, params: dict) -> str:
        if not self._client:
            return self._not_configured()
        page_id = (params.get("page_id") or "").strip()
        props_in: dict = params.get("properties") or {}
        if not page_id or not isinstance(props_in, dict) or not props_in:
            return json.dumps({"error": "page_id and non-empty properties are required"},
                              ensure_ascii=False)

        from engine.errors import map_exception
        from engine.markdown import markdown_to_rich_text

        properties: dict[str, Any] = {}
        for name, value in props_in.items():
            if isinstance(value, dict):
                properties[name] = value
            elif isinstance(value, bool):
                properties[name] = {"checkbox": value}
            elif isinstance(value, (int, float)):
                properties[name] = {"number": value}
            elif isinstance(value, list):
                properties[name] = {"multi_select": [{"name": str(v)} for v in value]}
            elif isinstance(value, str):
                properties[name] = {"rich_text": markdown_to_rich_text(value)}
            elif value is None:
                continue
            else:
                properties[name] = {"rich_text": markdown_to_rich_text(str(value))}

        try:
            updated = await self._client.pages.update(
                page_id=page_id, properties=properties,
            )
            return json.dumps({
                "status": "ok",
                "id": updated.get("id"),
                "url": updated.get("url"),
                "updated_properties": list(properties.keys()),
            }, ensure_ascii=False)
        except Exception as e:
            logger.exception("notion_update_page_properties failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)

    # ── Get database schema ───────────────────────────────────────

    @tool(
        name="notion_get_database_schema",
        description=(
            "List a Notion database's properties and their types. ALWAYS call this before "
            "notion_create_page(parent_database_id=...) or notion_query_database() so you "
            "know valid column names and value types. Also returns the title column name."
        ),
        parameters={
            "type": "object",
            "required": ["database_id"],
            "properties": {
                "database_id": {"type": "string"},
            },
        },
    )
    async def notion_get_database_schema(self, params: dict) -> str:
        if not self._client:
            return self._not_configured()
        db_id = (params.get("database_id") or "").strip()
        if not db_id:
            return json.dumps({"error": "database_id is required"}, ensure_ascii=False)

        from engine.errors import map_exception
        try:
            # 2025-09-03: databases.retrieve returns a *shell* with data_sources[];
            # the real property schema lives on the data source.
            db = await self._client.databases.retrieve(database_id=db_id)
            data_sources = db.get("data_sources") or []
            if not data_sources:
                return json.dumps({
                    "status": "error",
                    "error": "Database returned no data_sources — try sharing again.",
                }, ensure_ascii=False)
            ds_id = data_sources[0]["id"]
            ds = await self._client.data_sources.retrieve(data_source_id=ds_id)
            schema = []
            title_col = None
            for name, p in (ds.get("properties") or {}).items():
                t = p.get("type")
                entry: dict[str, Any] = {"name": name, "type": t}
                if t == "select" and isinstance(p.get("select"), dict):
                    entry["options"] = [
                        o.get("name") for o in (p["select"].get("options") or [])
                    ]
                elif t == "multi_select" and isinstance(p.get("multi_select"), dict):
                    entry["options"] = [
                        o.get("name") for o in (p["multi_select"].get("options") or [])
                    ]
                elif t == "status" and isinstance(p.get("status"), dict):
                    entry["options"] = [
                        o.get("name") for o in (p["status"].get("options") or [])
                    ]
                if t == "title":
                    title_col = name
                schema.append(entry)

            return json.dumps({
                "status": "ok",
                "id": db.get("id"),
                "data_source_id": ds_id,
                "title": self._extract_title(db) or "(untitled)",
                "url": db.get("url"),
                "title_property": title_col,
                "properties": schema,
                "note": (
                    "Database schema resolved via data_source. "
                    "Use data_source_id internally if needed; notion_create_page "
                    "and notion_query_database auto-resolve this."
                ),
            }, ensure_ascii=False)
        except Exception as e:
            logger.exception("notion_get_database_schema failed")
            return json.dumps({"status": "error", **map_exception(e)}, ensure_ascii=False)
