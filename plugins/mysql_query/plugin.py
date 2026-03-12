"""Standalone MySQL SELECT-only query tool plugin."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from qanot.plugins.base import Plugin, ToolDef

logger = logging.getLogger(__name__)


class QanotPlugin(Plugin):
    """MySQL query plugin — SELECT-only, 500-row limit, 5s timeout."""

    name = "mysql_query"
    description = "MySQL bazaga SELECT so'rov yuborish"
    tools_md = ""
    soul_append = ""

    def __init__(self):
        self._pool = None

    async def setup(self, config: dict) -> None:
        host = config.get("db_host", "")
        user = config.get("db_user", "")
        password = config.get("db_password", "")
        db = config.get("db_name", "")
        port = int(config.get("db_port", 3306))

        if not host or not user or not password or not db:
            logger.warning("[mysql_query] Missing DB config")
            return

        try:
            import aiomysql
            self._pool = await aiomysql.create_pool(
                host=host, port=port, user=user, password=password,
                db=db, maxsize=2, connect_timeout=5,
            )
            logger.info("[mysql_query] DB pool created for %s@%s/%s", user, host, db)
        except Exception as e:
            logger.error("[mysql_query] DB pool failed: %s", e)

    async def teardown(self) -> None:
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()

    def get_tools(self) -> list[ToolDef]:
        if not self._pool:
            return []

        async def mysql_query(p: dict) -> str:
            query = (p.get("query", "") or "").strip()
            if not re.match(r"^SELECT\b", query, re.IGNORECASE):
                return json.dumps({"error": "Faqat SELECT so'rovlar ruxsat etilgan."})
            blocked = re.compile(
                r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|EXEC|CALL)\b",
                re.IGNORECASE,
            )
            if blocked.search(query):
                return json.dumps({"error": "Bu turdagi so'rov ruxsat etilmagan."})
            try:
                async with self._pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("SET SESSION MAX_EXECUTION_TIME = 5000")
                        await cur.execute(query)
                        rows = await cur.fetchall()
                        cols = [d[0] for d in cur.description] if cur.description else []
                        result_rows = [dict(zip(cols, row)) for row in rows]
                        limited = result_rows[:500]
                        return json.dumps({
                            "rows": limited,
                            "total_rows": len(result_rows),
                            "truncated": len(result_rows) > 500,
                        }, indent=2, ensure_ascii=False, default=str)
            except Exception as e:
                return json.dumps({"error": str(e)})

        return [ToolDef(
            name="mysql_query",
            description="MySQL bazaga SELECT so'rov. Faqat SELECT. Max 500 qator, 5s timeout.",
            parameters={"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string", "description": "SQL SELECT so'rov"},
            }},
            handler=mysql_query,
        )]
