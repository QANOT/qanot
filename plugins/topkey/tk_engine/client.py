"""HTTP client for the TopKey HR & Project Management API.

Wraps the Laravel Sanctum-protected REST API at {api_url}/api/v1/*.
Login on demand, cache the bearer token in memory, re-login once on 401.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

API_BASE = "/api/v1"


class TopKeyAuthError(RuntimeError):
    """Authentication failed (bad credentials or token rejected after re-login)."""


class TopKeyAPIError(RuntimeError):
    """Non-auth API error returned by TopKey (4xx/5xx with a message)."""


class TopKeyClient:
    """Async HTTP client for TopKey REST API."""

    def __init__(self, base_url: str, email: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.token: str | None = None
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def login(self) -> None:
        """POST /api/v1/auth/login — cache the bearer token.

        TopKey returns: {"message": "...", "data": {"token": "...", "user": {...}}}
        """
        session = await self._get_session()
        url = f"{self.base_url}{API_BASE}/auth/login"
        async with session.post(
            url,
            json={"email": self.email, "password": self.password},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        ) as resp:
            try:
                data = await resp.json()
            except Exception as e:
                text = await resp.text()
                raise TopKeyAuthError(f"Login response not JSON: {e}; body={text[:200]}")
            if resp.status >= 400:
                msg = data.get("message") if isinstance(data, dict) else None
                raise TopKeyAuthError(msg or f"Login HTTP {resp.status}: {json.dumps(data)[:200]}")
        token = self._extract_token(data)
        if not token:
            raise TopKeyAuthError(f"Login: no token in response: {json.dumps(data)[:200]}")
        self.token = token

    @staticmethod
    def _extract_token(data: Any) -> str | None:
        """Token can be at data.token (Froiden) or top-level token (legacy)."""
        if not isinstance(data, dict):
            return None
        inner = data.get("data")
        if isinstance(inner, dict) and inner.get("token"):
            return str(inner["token"])
        if data.get("token"):
            return str(data["token"])
        return None

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: dict | None = None, params: dict | None = None) -> Any:
        return await self._request("POST", path, body=body, params=params)

    async def put(self, path: str, body: dict | None = None, params: dict | None = None) -> Any:
        return await self._request("PUT", path, body=body, params=params)

    async def delete(self, path: str, params: dict | None = None) -> Any:
        return await self._request("DELETE", path, params=params)

    async def get_all(
        self,
        path: str,
        params: dict | None = None,
        *,
        max_pages: int = 50,
        max_items: int = 5000,
    ) -> dict:
        """Walk paginated list endpoints, capped to keep responses bounded.

        Handles two TopKey paginations observed in the live API:

        1. **Froiden** (e.g. ``/task`` -> {"data":[...], "meta":{"paging":
           {"links": {"next": "<url>"}, "total": N}}}). Follows
           ``meta.paging.links.next`` until exhausted; the server hardcodes
           ``limit=10`` regardless of ``per_page``, hence the higher
           max_pages default.
        2. **Mobile** (e.g. ``/mobile/leave`` -> {"data":[...], "meta":
           {"current_page", "last_page", "total"}}). Bumps ``page`` until
           ``last_page``.
        """
        all_items: list = []
        total = 0
        next_url: str | None = None
        # First page: respect caller's params; per_page set high in case the
        # endpoint honours it (most don't, but it's free to try).
        first_params = {**(params or {}), "page": 1}
        first_params.setdefault("per_page", 200)
        # Track our own page counter for mobile-style endpoints that return
        # last_page but not current_page in the meta block.
        page_counter = 1
        # Froiden's `meta.paging.links.next` URL does NOT preserve our
        # `?fields=` (or other) query params — the server rebuilds the link
        # from scratch. We re-merge caller params onto each follow-up URL so
        # custom field selection is consistent across pages.
        persist_params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}

        for _ in range(max_pages):
            if next_url is None:
                data = await self.get(path, first_params)
            else:
                data = await self._request("GET", next_url, absolute=True, params=persist_params or None)

            items, meta = self._unwrap_list(data)
            all_items.extend(items)

            if meta:
                t = meta.get("total") or (meta.get("paging") or {}).get("total")
                if t is not None:
                    try:
                        total = int(t)
                    except (TypeError, ValueError):
                        pass

            if len(all_items) >= max_items:
                all_items = all_items[:max_items]
                break

            # Detect next link: Froiden (`meta.paging.links.next`) first.
            paging = meta.get("paging") if isinstance(meta, dict) else None
            if isinstance(paging, dict):
                links = paging.get("links") or {}
                next_url = links.get("next") or None
                if not next_url:
                    break
                continue

            # Mobile-style: meta has `last_page` (and optionally `current_page`).
            last = meta.get("last_page") if isinstance(meta, dict) else None
            current = meta.get("current_page") if isinstance(meta, dict) else None
            effective_current = int(current) if current is not None else page_counter
            if last is not None and effective_current < int(last):
                page_counter = effective_current + 1
                next_url = None
                first_params = {**(params or {}), "page": page_counter, "per_page": 200}
                continue

            # No more pages signalled.
            break

        return {"items": all_items, "total": total or len(all_items)}

    @staticmethod
    def _unwrap_list(data: Any) -> tuple[list, dict]:
        """Extract (items, meta) from common response shapes.

        Recognized shapes:
          - {"data": [...], "meta": {...}}              (Mobile controllers)
          - {"data": [...], "total": N, ...}            (Froiden RestAPI)
          - {"data": {"data": [...], "meta": {...}}}    (double-wrapped)
          - [...]                                       (raw list)
        """
        if isinstance(data, list):
            return data, {}
        if not isinstance(data, dict):
            return [], {}
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        # Froiden RestAPI flattens total/limit/offset into the top object.
        if not meta and any(k in data for k in ("total", "limit", "offset")):
            meta = {
                "total": data.get("total"),
                "limit": data.get("limit"),
                "offset": data.get("offset"),
            }
        items: list = []
        inner = data.get("data")
        if isinstance(inner, list):
            items = inner
        elif isinstance(inner, dict):
            # Double-wrapped: data.data is the list
            if isinstance(inner.get("data"), list):
                items = inner["data"]
                if isinstance(inner.get("meta"), dict):
                    meta = inner["meta"]
        return items, meta

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
        absolute: bool = False,
    ) -> Any:
        # First attempt; if 401 (token expired/missing), re-login once and retry.
        status, data = await self._raw(method, path, body=body, params=params, absolute=absolute)
        if status == 401:
            await self.login()
            status, data = await self._raw(method, path, body=body, params=params, absolute=absolute)
        if status >= 400:
            msg = None
            if isinstance(data, dict):
                msg = data.get("message") or data.get("error")
            raise TopKeyAPIError(msg or f"HTTP {status}: {json.dumps(data)[:200]}")
        return data

    async def _raw(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
        absolute: bool = False,
    ) -> tuple[int, Any]:
        session = await self._get_session()
        # `absolute=True` means caller provides a full URL (e.g. the
        # `meta.paging.links.next` URL from a Froiden response). Otherwise
        # we prepend the configured base + API_BASE prefix.
        url = path if absolute else f"{self.base_url}{API_BASE}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None and v != ""}
        async with session.request(
            method,
            url,
            json=body if method in ("POST", "PUT", "PATCH") else None,
            params=clean_params or None,
            headers=headers,
        ) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = {"message": (await resp.text())[:200]}
            return resp.status, data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
