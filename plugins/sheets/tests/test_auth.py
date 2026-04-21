"""Unit tests for engine.auth TokenManager.

We stub aiohttp.ClientSession so no network call is made.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from engine.auth import TokenManager, TokenRefreshError  # noqa: E402


class _FakeResp:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body)


class _FakeSession:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.calls = 0
        self.last_payload: Any = None

    def post(self, url, *, data=None, timeout=None):
        self.calls += 1
        self.last_payload = data
        return self._resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


def _patch_session(resp: _FakeResp):
    """Return a patch context that replaces aiohttp.ClientSession."""
    session = _FakeSession(resp)
    return patch("engine.auth.aiohttp.ClientSession", return_value=session), session


def test_refresh_returns_access_token():
    patcher, session = _patch_session(_FakeResp(200, {
        "access_token": "ya29.fresh",
        "expires_in": 3600,
        "token_type": "Bearer",
    }))
    with patcher:
        tm = TokenManager("rt", "cid", "csecret")
        token = asyncio.run(tm.get_access_token())
    assert token == "ya29.fresh"
    assert session.calls == 1
    # Payload must include grant_type=refresh_token and the refresh_token itself
    assert session.last_payload["grant_type"] == "refresh_token"
    assert session.last_payload["refresh_token"] == "rt"
    assert session.last_payload["client_id"] == "cid"


def test_cached_token_not_refreshed_twice():
    patcher, session = _patch_session(_FakeResp(200, {
        "access_token": "ya29.cached",
        "expires_in": 3600,
    }))
    with patcher:
        tm = TokenManager("rt", "cid", "csecret")
        t1 = asyncio.run(tm.get_access_token())
        t2 = asyncio.run(tm.get_access_token())
    assert t1 == t2 == "ya29.cached"
    assert session.calls == 1  # second call served from cache


def test_invalidate_forces_refresh():
    patcher, session = _patch_session(_FakeResp(200, {
        "access_token": "ya29.one",
        "expires_in": 3600,
    }))
    with patcher:
        tm = TokenManager("rt", "cid", "csecret")
        asyncio.run(tm.get_access_token())
        tm.invalidate()
        asyncio.run(tm.get_access_token())
    assert session.calls == 2


def test_revoked_refresh_token_raises():
    patcher, _ = _patch_session(_FakeResp(400, {
        "error": "invalid_grant",
        "error_description": "Token has been expired or revoked.",
    }))
    with patcher:
        tm = TokenManager("rt-revoked", "cid", "csecret")
        with pytest.raises(TokenRefreshError) as exc:
            asyncio.run(tm.get_access_token())
    assert "revoked" in str(exc.value).lower() or "400" in str(exc.value)


def test_expired_token_triggers_refresh():
    patcher, session = _patch_session(_FakeResp(200, {
        "access_token": "ya29.rotated",
        "expires_in": 3600,
    }))
    with patcher:
        tm = TokenManager("rt", "cid", "csecret")
        # Simulate a token that expired in the past
        tm._access_token = "ya29.stale"
        tm._expires_at = time.time() - 10
        token = asyncio.run(tm.get_access_token())
    assert token == "ya29.rotated"
    assert session.calls == 1
