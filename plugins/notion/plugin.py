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
# least one copy survives for later `from nt_engine.X import Y` calls.
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
            from nt_engine.client import make_client
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
        from nt_engine.errors import map_exception

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
        from nt_engine.client import iterate_paginated
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

        from nt_engine.errors import map_exception
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

        from nt_engine.errors import map_exception
        from nt_engine.markdown import blocks_to_markdown

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

        from nt_engine.errors import map_exception
        from nt_engine.markdown import markdown_to_blocks

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

        from nt_engine.errors import map_exception
        from nt_engine.markdown import markdown_to_blocks, markdown_to_rich_text

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

        from nt_engine.errors import map_exception
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

        from nt_engine.errors import map_exception
        from nt_engine.markdown import markdown_to_rich_text

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

        from nt_engine.errors import map_exception
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

    # ── Create database ──────────────────────────────────────────

    @tool(
        name="notion_create_database",
        description=(
            "Create a new Notion database (table) as a subpage of a parent page. "
            "The integration must be shared to the parent page first (via Notion "
            "UI: Add connections → pick Qanot).\n\n"
            "The `properties` argument defines columns. Accepts AGENT-FRIENDLY "
            "shortcuts — you don't need to know Notion's nested JSON:\n"
            "  - \"title\" → title column (every DB needs exactly one)\n"
            "  - \"text\" or \"rich_text\" → multi-line text\n"
            "  - \"number\" or {\"type\":\"number\",\"format\":\"dollar\"} → number with optional format\n"
            "    (formats: number, number_with_commas, percent, dollar, euro, …)\n"
            "  - {\"type\":\"select\",\"options\":[\"A\",\"B\"]} → single-select\n"
            "  - {\"type\":\"multi_select\",\"options\":[...]} → multi-select\n"
            "  - {\"type\":\"status\",\"options\":[...]} → status column\n"
            "  - \"date\", \"checkbox\", \"url\", \"email\", \"phone_number\", \"people\", \"files\"\n"
            "\nReturns database_id + data_source_id + URL."
        ),
        parameters={
            "type": "object",
            "required": ["parent_page_id", "title", "properties"],
            "properties": {
                "parent_page_id": {
                    "type": "string",
                    "description": "Page UUID that will own the new database (must be shared with the integration).",
                },
                "title": {
                    "type": "string",
                    "description": "Database title (e.g. 'Mijozlar 2026').",
                },
                "description": {
                    "type": "string",
                    "description": "Optional short description shown under the title.",
                },
                "properties": {
                    "type": "object",
                    "description": (
                        "Column definitions as {column_name: type_or_spec}. "
                        "EXACTLY ONE column must be type 'title'. See tool "
                        "description for shortcut formats."
                    ),
                },
                "is_inline": {
                    "type": "boolean",
                    "description": "Render inline inside the parent page (default true).",
                },
            },
        },
    )
    async def notion_create_database(self, params: dict) -> str:
        if not self._client:
            return self._not_configured()
        parent_page_id = (params.get("parent_page_id") or "").strip()
        title = (params.get("title") or "").strip()
        props_in = params.get("properties") or {}

        if not parent_page_id or not title:
            return json.dumps(
                {"error": "parent_page_id and title are required"},
                ensure_ascii=False,
            )
        if not isinstance(props_in, dict) or not props_in:
            return json.dumps(
                {"error": "properties must be a non-empty object"},
                ensure_ascii=False,
            )

        normalized = _normalise_db_properties(props_in)
        if isinstance(normalized, str):
            return json.dumps({"error": normalized}, ensure_ascii=False)

        # Validate exactly one title column
        title_cols = [
            k for k, v in normalized.items()
            if isinstance(v, dict) and v.get("type") == "title"
        ]
        if len(title_cols) != 1:
            return json.dumps(
                {
                    "error": (
                        f"Exactly one column must be type 'title', found {len(title_cols)}: "
                        f"{title_cols}. Add a single 'title' column or remove duplicates."
                    ),
                },
                ensure_ascii=False,
            )

        from nt_engine.errors import map_exception

        payload: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "initial_data_source": {"properties": normalized},
            "is_inline": bool(params.get("is_inline", True)),
        }
        if params.get("description"):
            payload["description"] = [
                {"type": "text", "text": {"content": str(params["description"])}},
            ]

        try:
            created = await self._client.databases.create(**payload)
            data_source_id = (
                (created.get("data_sources") or [{}])[0].get("id")
            )
            return json.dumps(
                {
                    "status": "ok",
                    "id": created.get("id"),
                    "data_source_id": data_source_id,
                    "title": title,
                    "url": created.get("url"),
                    "property_count": len(normalized),
                    "title_column": title_cols[0],
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("notion_create_database failed")
            return json.dumps(
                {"status": "error", **map_exception(e)}, ensure_ascii=False,
            )

    # ── Update database schema ───────────────────────────────────

    @tool(
        name="notion_update_database",
        description=(
            "Modify a Notion database: change title/description, add new columns, "
            "remove columns (set to null), or add options to existing select/"
            "multi_select/status columns.\n\n"
            "`add_properties`: columns to add/modify — same shortcut format as "
            "notion_create_database.\n"
            "`remove_properties`: array of column names to delete (data in those "
            "cells is LOST — Notion has no undo).\n\n"
            "Note: renaming a column in place isn't supported via this tool — "
            "remove the old column then add a new one (you'll lose the data), or "
            "rename it in Notion's UI."
        ),
        parameters={
            "type": "object",
            "required": ["database_id"],
            "properties": {
                "database_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "add_properties": {
                    "type": "object",
                    "description": "Columns to add or modify (same shortcut format as create).",
                },
                "remove_properties": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Column names to remove. Irreversible.",
                },
            },
        },
    )
    async def notion_update_database(self, params: dict) -> str:
        if not self._client:
            return self._not_configured()
        db_id = (params.get("database_id") or "").strip()
        if not db_id:
            return json.dumps({"error": "database_id is required"}, ensure_ascii=False)

        from nt_engine.errors import map_exception

        # Parts 1 & 2: title/description go on the DATABASE
        db_updates: dict[str, Any] = {}
        if params.get("title"):
            db_updates["title"] = [
                {"type": "text", "text": {"content": str(params["title"])}},
            ]
        if params.get("description"):
            db_updates["description"] = [
                {"type": "text", "text": {"content": str(params["description"])}},
            ]

        # Parts 3 & 4: property changes go on the DATA SOURCE (2025-09-03 API).
        # Add/modify passes a dict; remove passes null for each key.
        property_patch: dict[str, Any] = {}
        add_props = params.get("add_properties") or {}
        if isinstance(add_props, dict) and add_props:
            normalised = _normalise_db_properties(add_props)
            if isinstance(normalised, str):
                return json.dumps({"error": normalised}, ensure_ascii=False)
            property_patch.update(normalised)

        remove_props = params.get("remove_properties") or []
        if isinstance(remove_props, list):
            for name in remove_props:
                if isinstance(name, str) and name:
                    property_patch[name] = None

        if not db_updates and not property_patch:
            return json.dumps(
                {"error": "nothing to update: provide title, description, add_properties, or remove_properties"},
                ensure_ascii=False,
            )

        try:
            result: dict[str, Any] = {"status": "ok", "database_id": db_id}

            if db_updates:
                updated_db = await self._client.databases.update(
                    database_id=db_id, **db_updates,
                )
                result["updated_db_fields"] = list(db_updates.keys())
                result["title"] = self._extract_title(updated_db) or None

            if property_patch:
                ds_id = await self._resolve_data_source_id(db_id)
                await self._client.data_sources.update(
                    data_source_id=ds_id,
                    properties=property_patch,
                )
                added = [k for k, v in property_patch.items() if v is not None]
                removed = [k for k, v in property_patch.items() if v is None]
                result["data_source_id"] = ds_id
                result["added_or_modified"] = added
                result["removed"] = removed

            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.exception("notion_update_database failed")
            return json.dumps(
                {"status": "error", **map_exception(e)}, ensure_ascii=False,
            )

    # ── Archive page (soft-delete) ───────────────────────────────

    @tool(
        name="notion_archive_page",
        description=(
            "Soft-delete a page. Works for any page type: a subpage, a database, "
            "or a row within a database. Notion has no hard delete via API — the "
            "page moves to the workspace's Trash and can be restored for 30 days "
            "from the Notion UI.\n\n"
            "Set archived=false to RESTORE a previously archived page (as long as "
            "it hasn't been purged from Trash)."
        ),
        parameters={
            "type": "object",
            "required": ["page_id"],
            "properties": {
                "page_id": {"type": "string"},
                "archived": {
                    "type": "boolean",
                    "description": "true = archive (default). false = restore.",
                },
            },
        },
    )
    async def notion_archive_page(self, params: dict) -> str:
        if not self._client:
            return self._not_configured()
        page_id = (params.get("page_id") or "").strip()
        if not page_id:
            return json.dumps({"error": "page_id is required"}, ensure_ascii=False)
        archived = bool(params.get("archived", True))

        from nt_engine.errors import map_exception

        try:
            result = await self._client.pages.update(
                page_id=page_id, archived=archived,
            )
            return json.dumps(
                {
                    "status": "ok",
                    "id": result.get("id"),
                    "archived": result.get("archived", archived),
                    "url": result.get("url"),
                    "hint": (
                        "Restorable from Notion Trash for 30 days."
                        if archived
                        else "Page restored."
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("notion_archive_page failed")
            return json.dumps(
                {"status": "error", **map_exception(e)}, ensure_ascii=False,
            )


# ── Module helpers ───────────────────────────────────────────────


_SIMPLE_TYPES = frozenset({
    "title", "rich_text", "date", "checkbox", "url", "email",
    "phone_number", "people", "files", "created_time", "last_edited_time",
    "created_by", "last_edited_by",
})


def _normalise_db_properties(props_in: dict) -> dict | str:
    """Translate shortcut property definitions into Notion's nested JSON.

    Accepts any of:
      - "title"                                  → {type: title, title: {}}
      - "rich_text" / "text"                     → {type: rich_text, rich_text: {}}
      - "number"                                 → {type: number, number: {format: number}}
      - {"type": "number", "format": "dollar"}   → {type: number, number: {format: dollar}}
      - {"type": "select", "options": ["A","B"]} → expanded to {options: [{name: "A"}, {name: "B"}]}
      - raw Notion property object (pass-through if "type" key + matching inner)

    Returns a dict of normalised properties, or a str error message.
    """
    out: dict[str, dict] = {}
    for name, spec in props_in.items():
        if not isinstance(name, str) or not name.strip():
            return f"property name must be a non-empty string, got {name!r}"

        # "text" is a common agent alias for "rich_text"
        if spec == "text":
            spec = "rich_text"

        # String shortcut: "title", "rich_text", "number", "date", ...
        if isinstance(spec, str):
            t = spec.strip()
            if t == "number":
                out[name] = {"type": "number", "number": {"format": "number"}}
                continue
            if t in _SIMPLE_TYPES:
                out[name] = {"type": t, t: {}}
                continue
            return f"unknown property type {t!r} for column {name!r}"

        if not isinstance(spec, dict):
            return f"property {name!r} must be a string or object, got {type(spec).__name__}"

        # If spec already looks like a raw Notion schema (has both 'type' and
        # matching inner key), pass it through unchanged.
        t = spec.get("type")
        if t and t in spec and isinstance(spec.get(t), dict):
            out[name] = spec
            continue

        if not t:
            return f"property {name!r} missing 'type' field"

        # Shortcut dict: {"type": "number", "format": "dollar"}
        if t == "number":
            fmt = spec.get("format") or "number"
            out[name] = {"type": "number", "number": {"format": fmt}}
            continue

        # {"type": "select"|"multi_select"|"status", "options": [...]}
        if t in ("select", "multi_select", "status"):
            raw_opts = spec.get("options") or []
            if not isinstance(raw_opts, list):
                return f"{name!r}.options must be an array"
            expanded: list[dict] = []
            for opt in raw_opts:
                if isinstance(opt, str):
                    expanded.append({"name": opt})
                elif isinstance(opt, dict) and opt.get("name"):
                    # Accept {name, color?} pass-through
                    expanded.append({
                        "name": opt["name"],
                        **({"color": opt["color"]} if opt.get("color") else {}),
                    })
                else:
                    return f"{name!r}.options[] entries must be string or {{name, color?}}"
            out[name] = {"type": t, t: {"options": expanded}}
            continue

        # Simple types passed via dict (e.g. {"type": "date"})
        if t in _SIMPLE_TYPES:
            out[name] = {"type": t, t: {}}
            continue

        return (
            f"property type {t!r} for {name!r} not supported by the shortcut "
            "normaliser. Pass a raw Notion property object instead."
        )
    return out
