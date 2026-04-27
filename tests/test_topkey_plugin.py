"""Tests for the TopKey HR + Project Management plugin.

Covers: setup with valid/missing config, tool URL/body construction, 401
re-login, pagination walking, error envelope, tool registration count.

The HTTP layer (aiohttp.ClientSession) is mocked end-to-end — no real
requests to topkey.uz are made.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Plugin uses `from tk_engine.client import ...` lazily; importing the plugin
# module via the regular package path is enough since `_import_client` adds
# the plugin dir to sys.path on demand.
PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "topkey"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from plugins.topkey.plugin import QanotPlugin  # noqa: E402
from tk_engine.client import TopKeyClient  # noqa: E402


# ── Fake aiohttp session ──────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int, payload: Any):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class _FakeSession:
    """Records every call and replays a queued list of (status, payload)."""

    def __init__(self, queue: list[tuple[int, Any]]):
        self.queue = list(queue)
        self.calls: list[dict] = []
        self.closed = False

    def request(self, method: str, url: str, *, json=None, params=None, headers=None):
        self.calls.append({
            "method": method, "url": url, "json": json,
            "params": params or {}, "headers": headers or {},
        })
        if not self.queue:
            return _FakeResponse(200, {"data": []})
        status, payload = self.queue.pop(0)
        return _FakeResponse(status, payload)

    def post(self, url, *, json=None, headers=None):
        return self.request("POST", url, json=json, params=None, headers=headers)

    async def close(self):
        self.closed = True


def _attach_session(client: TopKeyClient, queue: list[tuple[int, Any]]) -> _FakeSession:
    session = _FakeSession(queue)
    client._session = session  # type: ignore[assignment]
    return session


def _login_payload() -> tuple[int, dict]:
    return (200, {"message": "ok", "data": {"token": "tok-123", "user": {"id": 7}}})


# ── 1. Setup with valid config logs in successfully ──────────


@pytest.mark.asyncio
async def test_setup_valid_config_logs_in():
    p = QanotPlugin()

    async def fake_login(self):
        self.token = "tok-123"

    with patch.object(TopKeyClient, "login", new=fake_login):
        await p.setup({
            "api_url": "https://topkey.uz",
            "email": "admin@example.com",
            "password": "secret",
            "workspace_dir": "/tmp/ws",
        })
    assert p.client is not None
    assert p.client.token == "tok-123"
    assert len(p.get_tools()) == 28


# ── 2. Setup with missing config logs warning, no crash ──────


@pytest.mark.asyncio
async def test_setup_missing_config_no_crash(caplog):
    p = QanotPlugin()
    await p.setup({"api_url": "", "email": "", "password": ""})
    assert p.client is None
    assert p.get_tools() == []


# ── 3. list_employees builds correct URL with filters ────────


@pytest.mark.asyncio
async def test_list_employees_url_and_filters():
    client = TopKeyClient("https://topkey.uz", "a@b.c", "x")
    client.token = "tok"
    session = _attach_session(client, [
        (200, {"data": [{"id": 1, "name": "Ali"}], "meta": {"total": 1}}),
    ])
    p = QanotPlugin()
    p.client = client
    tool = next(t for t in p.get_tools() if t.name == "topkey_list_employees")
    raw = await tool.handler({"department_id": 5, "status": "active", "page": 2})
    parsed = json.loads(raw)
    assert "error" not in parsed
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://topkey.uz/api/v1/employee"
    # Params get stringified by the client.
    assert call["params"]["department_id"] == "5"
    assert call["params"]["status"] == "active"
    assert call["params"]["page"] == "2"
    assert call["headers"]["Authorization"] == "Bearer tok"


# ── 4. get_employee 404 surfaces as error envelope ───────────


@pytest.mark.asyncio
async def test_get_employee_404_returns_error_envelope():
    client = TopKeyClient("https://topkey.uz", "a@b.c", "x")
    client.token = "tok"
    _attach_session(client, [
        (404, {"message": "Employee not found"}),
    ])
    p = QanotPlugin()
    p.client = client
    tool = next(t for t in p.get_tools() if t.name == "topkey_get_employee")
    parsed = json.loads(await tool.handler({"employee_id": 9999}))
    assert "error" in parsed
    assert "not found" in parsed["error"].lower()


# ── 5. create_task POSTs the right body ──────────────────────


@pytest.mark.asyncio
async def test_create_task_post_body():
    client = TopKeyClient("https://topkey.uz", "a@b.c", "x")
    client.token = "tok"
    session = _attach_session(client, [
        (200, {"message": "Task created", "data": {"id": 42, "heading": "Fix bug"}}),
    ])
    p = QanotPlugin()
    p.client = client
    tool = next(t for t in p.get_tools() if t.name == "topkey_create_task")
    raw = await tool.handler({
        "title": "Fix bug", "project_id": 3,
        "assigned_to": 11, "due_date": "2026-05-01", "priority": "high",
    })
    parsed = json.loads(raw)
    assert parsed["data"]["id"] == 42
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://topkey.uz/api/v1/task"
    body = call["json"]
    assert body["heading"] == "Fix bug"
    assert body["project_id"] == 3
    assert body["user_id"] == 11
    assert body["due_date"] == "2026-05-01"
    assert body["priority"] == "high"


# ── 6. approve_leave PUTs the right path ─────────────────────


@pytest.mark.asyncio
async def test_approve_leave_put_path():
    client = TopKeyClient("https://topkey.uz", "a@b.c", "x")
    client.token = "tok"
    session = _attach_session(client, [
        (200, {"message": "Leave approved", "data": {"id": 17, "status": "approved"}}),
    ])
    p = QanotPlugin()
    p.client = client
    tool = next(t for t in p.get_tools() if t.name == "topkey_approve_leave")
    parsed = json.loads(await tool.handler({"leave_id": 17, "approve_reason": "OK"}))
    assert parsed["data"]["status"] == "approved"
    call = session.calls[0]
    assert call["method"] == "PUT"
    assert call["url"] == "https://topkey.uz/api/v1/mobile/leave/17/approve"
    assert call["json"]["approve_reason"] == "OK"


# ── 7. Token re-login on 401 ─────────────────────────────────


@pytest.mark.asyncio
async def test_401_triggers_relogin_and_retry():
    client = TopKeyClient("https://topkey.uz", "a@b.c", "x")
    client.token = "stale-token"
    # Sequence: first GET → 401, then login POST → token, then GET retry → 200.
    session = _attach_session(client, [
        (401, {"message": "Unauthenticated"}),
        _login_payload(),
        (200, {"data": [{"id": 1}], "meta": {"total": 1}}),
    ])
    p = QanotPlugin()
    p.client = client
    tool = next(t for t in p.get_tools() if t.name == "topkey_list_employees")
    parsed = json.loads(await tool.handler({}))
    assert "error" not in parsed
    methods_urls = [(c["method"], c["url"]) for c in session.calls]
    assert methods_urls == [
        ("GET", "https://topkey.uz/api/v1/employee"),
        ("POST", "https://topkey.uz/api/v1/auth/login"),
        ("GET", "https://topkey.uz/api/v1/employee"),
    ]
    # Retry must use the freshly minted token, not the stale one.
    assert session.calls[2]["headers"]["Authorization"] == "Bearer tok-123"


# ── 8. Pagination: list_projects all=true walks 3 pages ─────


@pytest.mark.asyncio
async def test_list_projects_pagination_walks_pages():
    client = TopKeyClient("https://topkey.uz", "a@b.c", "x")
    client.token = "tok"
    session = _attach_session(client, [
        (200, {"data": [{"id": 1}, {"id": 2}], "meta": {"total": 5, "last_page": 3}}),
        (200, {"data": [{"id": 3}, {"id": 4}], "meta": {"total": 5, "last_page": 3}}),
        (200, {"data": [{"id": 5}], "meta": {"total": 5, "last_page": 3}}),
    ])
    p = QanotPlugin()
    p.client = client
    tool = next(t for t in p.get_tools() if t.name == "topkey_list_projects")
    parsed = json.loads(await tool.handler({"all": True}))
    assert parsed["total"] == 5
    assert [it["id"] for it in parsed["items"]] == [1, 2, 3, 4, 5]
    assert len(session.calls) == 3
    pages = [c["params"]["page"] for c in session.calls]
    assert pages == ["1", "2", "3"]


# ── 9. Bad input returns error envelope ──────────────────────


@pytest.mark.asyncio
async def test_missing_required_param_returns_error():
    client = TopKeyClient("https://topkey.uz", "a@b.c", "x")
    client.token = "tok"
    _attach_session(client, [])  # no HTTP calls expected
    p = QanotPlugin()
    p.client = client
    # create_task missing project_id
    tool = next(t for t in p.get_tools() if t.name == "topkey_create_task")
    parsed = json.loads(await tool.handler({"title": "x"}))
    assert parsed.get("error") and "project_id" in parsed["error"]


# ── 10. All 28 tools registered with required fields ────────


@pytest.mark.asyncio
async def test_all_28_tools_registered():
    client = TopKeyClient("https://topkey.uz", "a@b.c", "x")
    client.token = "tok"
    p = QanotPlugin()
    p.client = client
    tools = p.get_tools()
    assert len(tools) == 28
    # Every tool has the required ToolDef fields.
    names = []
    for t in tools:
        assert t.name and t.name.startswith("topkey_")
        assert t.description
        assert isinstance(t.parameters, dict)
        assert callable(t.handler)
        names.append(t.name)
    # No duplicate names.
    assert len(set(names)) == len(names)
    # Spot-check a few critical names exist (1 per domain).
    expected_subset = {
        "topkey_list_employees",  # employees
        "topkey_get_today_attendance",  # attendance
        "topkey_create_leave_request",  # leave
        "topkey_list_projects",  # projects
        "topkey_create_task", "topkey_update_task_status",  # tasks
        "topkey_log_time",  # time
        "topkey_login", "topkey_get_current_user", "topkey_list_users",  # auth
    }
    assert expected_subset.issubset(set(names))


# ── 11. SecretRef password resolution ────────────────────────


@pytest.mark.asyncio
async def test_password_secretref_env_resolved(monkeypatch):
    monkeypatch.setenv("TOPKEY_TEST_PWD", "from-env")
    p = QanotPlugin()
    captured = {}

    async def fake_login(self):
        captured["pw"] = self.password
        self.token = "tok"

    with patch.object(TopKeyClient, "login", new=fake_login):
        await p.setup({
            "api_url": "https://topkey.uz",
            "email": "a@b.c",
            "password": {"env": "TOPKEY_TEST_PWD"},
        })
    assert p.client is not None
    assert captured["pw"] == "from-env"


# ── 12. log_time POSTs to /timelog with right body ──────────


@pytest.mark.asyncio
async def test_log_time_post_body_and_validation():
    client = TopKeyClient("https://topkey.uz", "a@b.c", "x")
    client.token = "tok"
    session = _attach_session(client, [
        (200, {"data": {"id": 99, "task_id": 5, "total_hours": 2.5}}),
    ])
    p = QanotPlugin()
    p.client = client
    tool = next(t for t in p.get_tools() if t.name == "topkey_log_time")

    # Missing hours → error.
    parsed = json.loads(await tool.handler({"task_id": 5}))
    assert parsed.get("error") and "hours" in parsed["error"]

    # Happy path.
    parsed = json.loads(await tool.handler({
        "task_id": 5, "hours": 2.5, "date": "2026-04-26", "memo": "review",
    }))
    assert parsed["data"]["id"] == 99
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://topkey.uz/api/v1/timelog"
    assert call["json"] == {
        "task_id": 5, "total_hours": 2.5, "date": "2026-04-26", "memo": "review",
    }
