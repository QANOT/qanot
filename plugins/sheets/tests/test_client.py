"""Unit tests for engine.client — stubs the aiohttp session.

We don't hit real Google endpoints. The SheetsClient is fed a fake session
that answers a scripted queue of responses so we can verify:
  - 401 triggers token invalidate + one retry
  - headers carry Bearer token
  - URL-encoding of A1 ranges (spaces, '!', ':')
  - search_values substring match + header-keyed rows
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from engine.client import SheetsAPIError, SheetsClient, _encode_range  # noqa: E402


# ── Fake aiohttp session ─────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body
        self._text = str(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def text(self):
        return self._text


class _FakeSession:
    """Records requests and replays scripted responses."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.closed = False

    def request(self, method, url, *, params=None, json=None, headers=None):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "json": json,
                "headers": headers or {},
            }
        )
        if not self._responses:
            raise AssertionError(f"No scripted response for {method} {url}")
        return self._responses.pop(0)

    async def close(self):
        self.closed = True


class _FakeTokens:
    """Token stub: increments a counter; returns 'tok-N' each call."""

    def __init__(self) -> None:
        self._n = 0
        self.invalidated = 0

    async def get_access_token(self) -> str:
        self._n += 1
        return f"tok-{self._n}"

    def invalidate(self) -> None:
        self.invalidated += 1


def _client_with(responses: list[_FakeResponse]) -> tuple[SheetsClient, _FakeSession, _FakeTokens]:
    session = _FakeSession(responses)
    tokens = _FakeTokens()
    client = SheetsClient(tokens)  # type: ignore[arg-type]
    # Inject our fake session, bypassing the lazy constructor
    client._session = session  # type: ignore[attr-defined]
    return client, session, tokens


# ── _encode_range ────────────────────────────────────────────────


def test_encode_range_escapes_bang_and_colon():
    assert _encode_range("Sheet1!A1:C10") == "Sheet1%21A1%3AC10"


def test_encode_range_escapes_spaces_and_cyrillic():
    # Uzbek tab names or quoted tab names with spaces.
    encoded = _encode_range("'Mening Ro'yxati'!A:C")
    assert " " not in encoded
    assert "!" not in encoded


# ── Auth header + basic GET ──────────────────────────────────────


def test_get_spreadsheet_passes_bearer_header():
    client, session, tokens = _client_with([
        _FakeResponse(200, {"spreadsheetId": "x", "sheets": []}),
    ])
    asyncio.run(client.get_spreadsheet("x"))
    assert session.requests[0]["headers"]["Authorization"] == "Bearer tok-1"
    assert session.requests[0]["url"].endswith("/spreadsheets/x")


# ── 401 retry with token refresh ─────────────────────────────────


def test_401_invalidates_token_and_retries_once():
    client, session, tokens = _client_with([
        _FakeResponse(401, {"error": "unauth"}),   # first attempt
        _FakeResponse(200, {"spreadsheetId": "x"}),  # retry succeeds
    ])
    result = asyncio.run(client.get_spreadsheet("x"))
    assert result["spreadsheetId"] == "x"
    assert tokens.invalidated == 1
    assert len(session.requests) == 2
    # Second attempt must use a fresh token (tok-2, not tok-1)
    assert session.requests[1]["headers"]["Authorization"] == "Bearer tok-2"


def test_401_twice_raises():
    client, session, tokens = _client_with([
        _FakeResponse(401, {"error": "unauth"}),
        _FakeResponse(401, {"error": "still unauth"}),
    ])
    with pytest.raises(SheetsAPIError) as exc:
        asyncio.run(client.get_spreadsheet("x"))
    assert exc.value.status == 401


def test_non_401_4xx_raises_immediately():
    client, session, tokens = _client_with([
        _FakeResponse(404, {"error": {"message": "Not found"}}),
    ])
    with pytest.raises(SheetsAPIError) as exc:
        asyncio.run(client.get_spreadsheet("missing"))
    assert exc.value.status == 404
    # Must NOT retry on non-401
    assert len(session.requests) == 1
    assert tokens.invalidated == 0


# ── Values: read / append / update ───────────────────────────────


def test_read_values_returns_empty_list_when_missing():
    client, *_ = _client_with([_FakeResponse(200, {"range": "Sheet1!A1:B2"})])
    values = asyncio.run(client.read_values("sid", "Sheet1!A1:B2"))
    assert values == []


def test_read_values_passes_encoded_range_in_url():
    client, session, _ = _client_with([
        _FakeResponse(200, {"values": [[1, 2], [3, 4]]}),
    ])
    values = asyncio.run(client.read_values("sid", "Tab One!A:B"))
    assert values == [[1, 2], [3, 4]]
    url = session.requests[0]["url"]
    assert "/sid/values/Tab%20One%21A%3AB" in url


def test_append_values_sends_insert_rows_param():
    client, session, _ = _client_with([_FakeResponse(200, {"updates": {"updatedRows": 1}})])
    asyncio.run(client.append_values("sid", "Log!A1", [["x", "y"]]))
    req = session.requests[0]
    assert req["method"] == "POST"
    assert req["params"] == {
        "valueInputOption": "USER_ENTERED",
        "insertDataOption": "INSERT_ROWS",
    }
    assert req["json"] == {"values": [["x", "y"]]}


def test_update_values_uses_PUT_and_correct_params():
    client, session, _ = _client_with([_FakeResponse(200, {"updatedCells": 2})])
    asyncio.run(client.update_values("sid", "Log!A1:B1", [["x", "y"]]))
    req = session.requests[0]
    assert req["method"] == "PUT"
    assert req["params"] == {"valueInputOption": "USER_ENTERED"}


# ── search_values: header-keyed rows ─────────────────────────────


def test_search_values_returns_header_keyed_rows_with_row_number():
    # Tab contents:
    #   row 1: headers
    #   row 2: Akmal
    #   row 3: Bobur
    #   row 4: AKMAL-jr (case-insensitive match)
    client, *_ = _client_with([
        _FakeResponse(200, {"values": [
            ["name", "amount", "note"],
            ["Akmal", 150, "naqd"],
            ["Bobur", 200, "card"],
            ["AKMAL-jr", 50, ""],
        ]}),
    ])
    matches = asyncio.run(client.search_values("sid", "Sales", "akmal"))
    assert len(matches) == 2
    # Row 2 (first data row)
    assert matches[0]["_row"] == 2
    assert matches[0]["name"] == "Akmal"
    assert matches[0]["amount"] == 150
    # Row 4 (AKMAL-jr)
    assert matches[1]["_row"] == 4


def test_search_values_respects_limit():
    rows = [["h"]] + [[f"match-{i}"] for i in range(10)]
    client, *_ = _client_with([_FakeResponse(200, {"values": rows})])
    matches = asyncio.run(client.search_values("sid", "T", "match", limit=3))
    assert len(matches) == 3


def test_search_values_empty_sheet_returns_empty():
    client, *_ = _client_with([_FakeResponse(200, {})])
    assert asyncio.run(client.search_values("sid", "T", "q")) == []


# ── create_spreadsheet with headers writes header row ────────────


def test_create_spreadsheet_with_headers_appends_row_one():
    client, session, _ = _client_with([
        _FakeResponse(200, {
            "spreadsheetId": "new",
            "spreadsheetUrl": "https://docs.google.com/xyz",
            "sheets": [{"properties": {"title": "Sheet1"}}],
        }),
        _FakeResponse(200, {"updates": {"updatedRows": 1}}),
    ])
    result = asyncio.run(client.create_spreadsheet(
        "My Sheet",
        headers=["Col A", "Col B"],
    ))
    assert result["spreadsheetId"] == "new"
    # Two requests: create then append headers to Sheet1!A1
    assert len(session.requests) == 2
    append_req = session.requests[1]
    assert append_req["method"] == "POST"
    assert append_req["json"] == {"values": [["Col A", "Col B"]]}
    assert "Sheet1%21A1" in append_req["url"]
