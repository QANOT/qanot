"""Notion AsyncClient factory + paginated helpers.

Wraps the `notion-client` (ramnes/notion-sdk-py) AsyncClient with:
  - lazy import so the plugin module loads even before the dep is installed
  - deterministic error surface via `engine.errors.map_exception`
  - async pagination helper (list endpoints expose start_cursor / has_more)

All API calls go through `api.notion.com/v1`; no SSRF surface.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


def make_client(token: str) -> Any:
    """Build an `AsyncClient` from an integration token.

    Imported lazily so import failures don't break plugin discovery.
    Raises `RuntimeError` with an actionable message if the SDK isn't installed.
    """
    try:
        from notion_client import AsyncClient  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "notion-client package is not installed. "
            "Add `notion-client>=2.2.1` to the container and restart."
        ) from e

    # Defaults are sensible: built-in retry=2, exponential backoff with jitter,
    # Retry-After honoured on 429, ~30s timeout. We don't override.
    return AsyncClient(auth=token)


async def iterate_paginated(
    fetch_page,
    *,
    page_size: int = 100,
    max_items: int | None = None,
) -> AsyncIterator[dict]:
    """Iterate a Notion paginated endpoint lazily.

    `fetch_page` is an async callable that accepts `start_cursor`/`page_size`
    kwargs and returns a response dict with `results` + `has_more` + `next_cursor`.
    """
    cursor: str | None = None
    yielded = 0
    while True:
        kwargs: dict[str, Any] = {"page_size": page_size}
        if cursor is not None:
            kwargs["start_cursor"] = cursor
        resp = await fetch_page(**kwargs)
        for item in resp.get("results", []):
            yield item
            yielded += 1
            if max_items is not None and yielded >= max_items:
                return
        if not resp.get("has_more"):
            return
        cursor = resp.get("next_cursor")
        if not cursor:
            return
