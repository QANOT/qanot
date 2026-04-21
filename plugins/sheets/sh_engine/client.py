"""Google Sheets v4 + Drive v3 REST wrapper.

Thin async client built on aiohttp. Intentionally avoids google-api-python-client
to keep the container small and to work cleanly with asyncio (that SDK is sync).

All requests carry Authorization: Bearer <access-token>. On 401 we invalidate
the token cache once and retry — covers the narrow window where a cached token
was revoked or rotated server-side.

Scope note: the plugin is built against drive.file. That means every request
will succeed only for spreadsheets the owner picked via Google Picker OR
spreadsheets this app itself created. Accessing anything else returns 404/403,
which we surface as a friendly Uzbek message in engine.errors.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from sh_engine.auth import TokenManager

logger = logging.getLogger(__name__)

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_BASE = "https://www.googleapis.com/drive/v3"

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)


class SheetsAPIError(Exception):
    """Raised on non-2xx response from Sheets or Drive API."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body if isinstance(body, dict) else {"raw": body}
        err = self.body.get("error", {}) if isinstance(self.body, dict) else {}
        message = err.get("message") if isinstance(err, dict) else None
        super().__init__(f"[{status}] {message or body}")


class SheetsClient:
    """Minimal async wrapper over Sheets v4 + Drive v3 REST."""

    def __init__(self, tokens: TokenManager) -> None:
        self._tokens = tokens
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
        return self._session

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: Any | None = None,
    ) -> dict:
        """Authenticated request with one 401-retry."""
        session = await self._get_session()
        for attempt in range(2):
            access = await self._tokens.get_access_token()
            headers = {"Authorization": f"Bearer {access}"}
            async with session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
            ) as resp:
                if resp.status == 401 and attempt == 0:
                    # Cached token may be stale (revoked, rotated); force refresh.
                    self._tokens.invalidate()
                    continue
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"raw": (await resp.text())[:400]}
                if resp.status >= 400:
                    raise SheetsAPIError(resp.status, data)
                return data if isinstance(data, dict) else {"result": data}
        # Shouldn't reach here — attempt loop either returns or raises.
        raise SheetsAPIError(401, {"error": "token refresh exhausted"})

    # ── Spreadsheet metadata / creation ──────────────────────────

    async def create_spreadsheet(
        self,
        title: str,
        *,
        tab_name: str | None = None,
        headers: list[str] | None = None,
    ) -> dict:
        """Create a spreadsheet owned by the user's Drive.

        If `headers` is provided, writes row 1 of the (first) tab.
        """
        payload: dict[str, Any] = {"properties": {"title": title}}
        if tab_name:
            payload["sheets"] = [{"properties": {"title": tab_name}}]
        created = await self._request("POST", SHEETS_BASE, json_body=payload)

        sid = created.get("spreadsheetId")
        if headers and sid:
            # Use first tab's title if caller didn't supply one.
            first_tab = (
                tab_name
                or created.get("sheets", [{}])[0]
                .get("properties", {})
                .get("title", "Sheet1")
            )
            await self.append_values(sid, f"{first_tab}!A1", [headers])
        return created

    async def get_spreadsheet(self, spreadsheet_id: str) -> dict:
        return await self._request(
            "GET",
            f"{SHEETS_BASE}/{spreadsheet_id}",
        )

    # ── Values ───────────────────────────────────────────────────

    async def read_values(
        self,
        spreadsheet_id: str,
        range_: str,
    ) -> list[list[Any]]:
        url = (
            f"{SHEETS_BASE}/{spreadsheet_id}/values/{_encode_range(range_)}"
        )
        data = await self._request("GET", url)
        return data.get("values", [])

    async def append_values(
        self,
        spreadsheet_id: str,
        range_: str,
        values: list[list[Any]],
        *,
        value_input_option: str = "USER_ENTERED",
    ) -> dict:
        url = (
            f"{SHEETS_BASE}/{spreadsheet_id}/values/"
            f"{_encode_range(range_)}:append"
        )
        return await self._request(
            "POST",
            url,
            params={
                "valueInputOption": value_input_option,
                "insertDataOption": "INSERT_ROWS",
            },
            json_body={"values": values},
        )

    async def update_values(
        self,
        spreadsheet_id: str,
        range_: str,
        values: list[list[Any]],
        *,
        value_input_option: str = "USER_ENTERED",
    ) -> dict:
        url = (
            f"{SHEETS_BASE}/{spreadsheet_id}/values/{_encode_range(range_)}"
        )
        return await self._request(
            "PUT",
            url,
            params={"valueInputOption": value_input_option},
            json_body={"values": values},
        )

    async def search_values(
        self,
        spreadsheet_id: str,
        tab: str,
        query: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Naive contains-match across the whole tab.

        Sheets API has no server-side text search; agent-friendly behaviour
        here is to pull the tab, scan rows, and return matches as dicts
        keyed by header row for easier downstream reasoning.
        """
        values = await self.read_values(spreadsheet_id, tab)
        if not values:
            return []
        header_row = values[0]
        headers_clean = [str(h) if h is not None else "" for h in header_row]
        q = query.lower()
        out: list[dict[str, Any]] = []
        for row_idx, row in enumerate(values[1:], start=2):
            # row_idx is 1-based sheet row number (header is row 1, data starts at 2)
            if any(q in str(cell).lower() for cell in row):
                if headers_clean:
                    row_dict: dict[str, Any] = {
                        headers_clean[i] if i < len(headers_clean) else f"col{i + 1}": v
                        for i, v in enumerate(row)
                    }
                else:
                    row_dict = {f"col{i + 1}": v for i, v in enumerate(row)}
                row_dict["_row"] = row_idx
                out.append(row_dict)
                if len(out) >= limit:
                    break
        return out

    # ── Drive permissions (share) ────────────────────────────────

    async def share(
        self,
        spreadsheet_id: str,
        email: str,
        *,
        role: str = "writer",
        send_notification: bool = True,
    ) -> dict:
        url = f"{DRIVE_BASE}/files/{spreadsheet_id}/permissions"
        return await self._request(
            "POST",
            url,
            params={"sendNotificationEmail": str(send_notification).lower()},
            json_body={
                "role": role,
                "type": "user",
                "emailAddress": email,
            },
        )


def _encode_range(range_: str) -> str:
    """URL-encode an A1 range. Spaces, !, : all need escaping in the path."""
    return quote(range_, safe="")
