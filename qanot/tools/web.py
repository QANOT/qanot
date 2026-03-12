"""Web search tool — Brave Search API integration."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import aiohttp

from qanot.agent import ToolRegistry

logger = logging.getLogger(__name__)

# Brave Search API
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_TIMEOUT = 15  # seconds

# In-memory cache
_cache: dict[str, tuple[float, str]] = {}
CACHE_TTL = 900  # 15 minutes
CACHE_MAX = 50


def _cache_get(key: str) -> str | None:
    """Get cached result if not expired."""
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, result = entry
    if time.monotonic() - ts > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return result


def _cache_set(key: str, result: str) -> None:
    """Cache a result, evicting oldest if over limit."""
    if len(_cache) >= CACHE_MAX:
        oldest_key = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest_key, None)
    _cache[key] = (time.monotonic(), result)


def _format_results(data: dict, query: str) -> str:
    """Format Brave API response into clean text for the LLM."""
    web = data.get("web", {})
    results = web.get("results", [])

    if not results:
        return json.dumps({
            "query": query,
            "results": [],
            "message": "No results found.",
        })

    formatted = []
    for r in results:
        entry: dict[str, Any] = {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "description": r.get("description", "").replace("<strong>", "").replace("</strong>", ""),
        }
        age = r.get("age")
        if age:
            entry["age"] = age
        formatted.append(entry)

    return json.dumps({
        "query": query,
        "source": "[web search — external content, may be inaccurate]",
        "count": len(formatted),
        "results": formatted,
    }, ensure_ascii=False)


def register_web_tools(
    registry: ToolRegistry,
    brave_api_key: str,
) -> None:
    """Register web search tools."""

    async def web_search(params: dict) -> str:
        """Search the web using Brave Search API."""
        query = params.get("query", "").strip()
        if not query:
            return json.dumps({"error": "Query is required"})

        count = min(int(params.get("count", 5)), 10)

        # Check cache
        cache_key = f"{query.lower()}:{count}"
        cached = _cache_get(cache_key)
        if cached:
            logger.debug("Web search cache hit: %s", query)
            return cached

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Accept": "application/json",
                    "X-Subscription-Token": brave_api_key,
                }
                api_params = {
                    "q": query,
                    "count": str(count),
                }
                async with session.get(
                    BRAVE_API_URL,
                    headers=headers,
                    params=api_params,
                    timeout=aiohttp.ClientTimeout(total=BRAVE_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error("Brave API error %d: %s", resp.status, error_text[:200])
                        return json.dumps({"error": f"Search API error ({resp.status})"})

                    data = await resp.json()
                    result = _format_results(data, query)
                    _cache_set(cache_key, result)
                    return result

        except aiohttp.ClientError as e:
            logger.error("Web search network error: %s", e)
            return json.dumps({"error": "Search request failed. Try again."})
        except Exception as e:
            logger.error("Web search error: %s", e)
            return json.dumps({"error": str(e)})

    registry.register(
        name="web_search",
        description=(
            "Search the web for current information. Use this for: real-time data "
            "(weather, news, prices, events), facts you're unsure about, "
            "anything that may have changed after your training data."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — be specific and concise",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results (1-10, default 5)",
                },
            },
            "required": ["query"],
        },
        handler=web_search,
    )
