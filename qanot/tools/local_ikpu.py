"""IKPU (MXIK) tovar kodi qidirish."""

from __future__ import annotations

import json
import logging

import aiohttp

from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)

# IKPU API — Tovar klassifikatori (bepul)
IKPU_URL = "https://tasnif.soliq.uz/api/cl-api/class/search"


def register_ikpu_tools(registry: ToolRegistry) -> None:
    """Register IKPU code search tools."""

    async def search_ikpu(params: dict) -> str:
        """IKPU (MXIK) tovar kodini qidirish."""
        query = params.get("query", "").strip()
        if not query:
            return json.dumps({"error": "Qidiruv so'zini kiriting"})
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    IKPU_URL,
                    params={"keyword": query, "page": 0, "size": 10, "lang": "uz"},
                    headers={"Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)

            items = data.get("content", data.get("data", []))
            if not items:
                return json.dumps({"error": f"'{query}' bo'yicha IKPU topilmadi"})

            results = []
            for item in items[:10]:
                results.append({
                    "code": item.get("mxikCode", item.get("code", "")),
                    "name": item.get("mxikFullNameUz", item.get("nameUz", item.get("name", ""))),
                    "units": item.get("unitName", ""),
                })
            return json.dumps({"query": query, "results": results}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"IKPU qidirishda xatolik: {e}"})

    registry.register(
        name="ikpu_search",
        description="Search IKPU (MXIK) product classification codes. Finds 17-digit IKPU codes by product name.",
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Tovar nomi (masalan: shakar, un, telefon)",
                },
            },
        },
        handler=search_ikpu,
    )
