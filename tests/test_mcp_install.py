"""Tests for agent-initiated MCP server install flow.

The flow is: agent calls mcp_propose → user gets Telegram approval card →
user clicks button → config.json is atomically mutated → bot restarts.

These tests mock the MCP transport layer (MCPManager.add_server) so we can
exercise the proposal/approval state machine without needing a real MCP server.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qanot.config import Config
from qanot.registry import ToolRegistry
from qanot.tools.mcp_manage import (
    AGENT_SOURCE_MARKER,
    PROPOSAL_TTL_SECONDS,
    _REGISTERED_REGISTRIES,
    handle_mcp_approve_callback,
    handle_mcp_remove_callback,
    register_mcp_tools,
)


# ──────────────────────────── fixtures / helpers ────────────────────────────


def _make_config(tmpdir: str, **overrides) -> Config:
    return Config(
        bot_token="test-bot-token",
        api_key="test-key",
        workspace_dir=tmpdir,
        sessions_dir=tmpdir + "/sessions",
        mcp_servers=overrides.get("mcp_servers", []),
    )


def _make_adapter() -> SimpleNamespace:
    """Build a fake TelegramAdapter with the attributes register_mcp_tools needs."""
    adapter = SimpleNamespace()
    adapter._pending_mcp_proposals = {}
    adapter._pending_mcp_removals = {}
    adapter.bot = MagicMock()
    adapter.bot.send_message = AsyncMock()
    return adapter


def _make_callback(user_id: int, data: str, message_text: str = "card") -> SimpleNamespace:
    """Build a fake aiogram CallbackQuery."""
    msg = SimpleNamespace()
    msg.text = message_text
    msg.edit_text = AsyncMock()
    cb = SimpleNamespace()
    cb.from_user = SimpleNamespace(id=user_id)
    cb.data = data
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


def _write_config_file(tmpdir: Path, **fields) -> Path:
    """Write a config.json with the given fields and point QANOT_CONFIG at it."""
    cfg_path = tmpdir / "config.json"
    data = {
        "bot_token": "test-bot-token",
        "api_key": "test-key",
        "mcp_servers": [],
    }
    data.update(fields)
    cfg_path.write_text(json.dumps(data, indent=2))
    return cfg_path


@pytest.fixture(autouse=True)
def _reset_registration_cache():
    """register_mcp_tools is idempotent via a module-level set — clear it
    between tests so each test gets a clean registration."""
    _REGISTERED_REGISTRIES.clear()
    yield
    _REGISTERED_REGISTRIES.clear()


# Mock MCPManager.add_server so no real MCP server is spawned.
# Returns (ok=True, tools=[...], error="") for any cfg by default.
def _patch_mcp_manager(tools=None, ok=True, error=""):
    tools = tools or [
        {"name": "fake_tool", "description": "A fake tool", "input_schema": {}},
    ]

    class _FakeMgr:
        def __init__(self):
            pass

        async def add_server(self, cfg, *, dry_run=False):
            return ok, list(tools), error

        async def disconnect_all(self):
            pass

    return patch("qanot.tools.mcp_manage.MCPManager", _FakeMgr)


def _patch_mcp_package():
    """Make `import mcp` succeed inside _require_mcp_package."""
    fake_mcp = MagicMock()
    return patch.dict("sys.modules", {"mcp": fake_mcp})


# ──────────────────────────── tests ────────────────────────────


class TestMcpTest:

    @pytest.mark.asyncio
    async def test_dry_run_lists_tools_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg_path = _write_config_file(tmp)
            config = _make_config(tmpdir)
            reg = ToolRegistry()
            adapter = _make_adapter()

            with patch.dict("os.environ", {"QANOT_CONFIG": str(cfg_path)}), \
                 _patch_mcp_package(), \
                 _patch_mcp_manager(tools=[{"name": "echo", "description": "Echo tool", "input_schema": {}}]):
                register_mcp_tools(
                    reg, config, None, adapter,
                    get_user_id=lambda: "42",
                    get_chat_id=lambda: 100,
                )
                result = await reg.execute("mcp_test", {
                    "name": "testsrv",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                })

            data = json.loads(result)
            assert data["success"] is True
            assert data["tools"][0]["name"] == "echo"

            # Config on disk unchanged
            on_disk = json.loads(cfg_path.read_text())
            assert on_disk["mcp_servers"] == []


class TestMcpPropose:

    @pytest.mark.asyncio
    async def test_stores_pending_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg_path = _write_config_file(tmp)
            config = _make_config(tmpdir)
            reg = ToolRegistry()
            adapter = _make_adapter()

            with patch.dict("os.environ", {"QANOT_CONFIG": str(cfg_path)}), \
                 _patch_mcp_package(), \
                 _patch_mcp_manager():
                register_mcp_tools(
                    reg, config, None, adapter,
                    get_user_id=lambda: "42",
                    get_chat_id=lambda: 100,
                )
                result = await reg.execute("mcp_propose", {
                    "name": "srv1",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                    "source": "user message",
                    "reason": "filesystem access",
                })

            data = json.loads(result)
            assert data["success"] is True
            pid = data["proposal_id"]
            pending = adapter._pending_mcp_proposals[pid]
            assert pending["user_id"] == 42
            assert pending["chat_id"] == 100
            assert pending["cfg"]["name"] == "srv1"
            assert pending["expires_at"] > 0
            # Telegram send_message was called with an approval card
            adapter.bot.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_disallowed_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg_path = _write_config_file(tmp)
            config = _make_config(tmpdir)
            reg = ToolRegistry()
            adapter = _make_adapter()

            with patch.dict("os.environ", {"QANOT_CONFIG": str(cfg_path)}), \
                 _patch_mcp_package(), \
                 _patch_mcp_manager():
                register_mcp_tools(
                    reg, config, None, adapter,
                    get_user_id=lambda: "42",
                    get_chat_id=lambda: 100,
                )
                result = await reg.execute("mcp_propose", {
                    "name": "srv1",
                    "transport": "stdio",
                    "command": "rm",
                    "args": ["-rf", "/"],
                    "source": "user message",
                    "reason": "destructive test",
                })

            data = json.loads(result)
            assert data["success"] is False
            assert "allowlist" in data["error"]
            assert adapter._pending_mcp_proposals == {}
            adapter.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_http_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg_path = _write_config_file(tmp)
            config = _make_config(tmpdir)
            reg = ToolRegistry()
            adapter = _make_adapter()

            with patch.dict("os.environ", {"QANOT_CONFIG": str(cfg_path)}), \
                 _patch_mcp_package(), \
                 _patch_mcp_manager():
                register_mcp_tools(
                    reg, config, None, adapter,
                    get_user_id=lambda: "42",
                    get_chat_id=lambda: 100,
                )
                result = await reg.execute("mcp_propose", {
                    "name": "srv1",
                    "transport": "sse",
                    "url": "http://insecure.example.com/sse",
                    "source": "user message",
                    "reason": "test",
                })

            data = json.loads(result)
            assert data["success"] is False
            assert "https" in data["error"].lower()


class TestMcpApproval:

    @pytest.mark.asyncio
    async def test_wrong_user_cannot_approve(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg_path = _write_config_file(tmp)
            config = _make_config(tmpdir)
            reg = ToolRegistry()
            adapter = _make_adapter()

            with patch.dict("os.environ", {"QANOT_CONFIG": str(cfg_path)}), \
                 _patch_mcp_package(), \
                 _patch_mcp_manager():
                register_mcp_tools(
                    reg, config, None, adapter,
                    get_user_id=lambda: "42",
                    get_chat_id=lambda: 100,
                )
                result = await reg.execute("mcp_propose", {
                    "name": "srv1",
                    "transport": "stdio",
                    "command": "npx",
                    "args": [],
                    "source": "user message",
                    "reason": "test",
                })
                pid = json.loads(result)["proposal_id"]

                # Someone else clicks approve
                cb = _make_callback(user_id=9999, data=f"mcp_approve:{pid}")
                with patch("qanot.tools.mcp_manage._trigger_restart") as restart_mock:
                    await handle_mcp_approve_callback(
                        adapter, config, cb, "approve", pid,
                    )
                restart_mock.assert_not_called()

            # Proposal still pending, config unchanged
            assert pid in adapter._pending_mcp_proposals
            on_disk = json.loads(cfg_path.read_text())
            assert on_disk["mcp_servers"] == []

    @pytest.mark.asyncio
    async def test_correct_user_writes_config_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg_path = _write_config_file(
                tmp,
                provider="anthropic",
                custom_field="keep-me",
            )
            config = _make_config(tmpdir)
            reg = ToolRegistry()
            adapter = _make_adapter()

            with patch.dict("os.environ", {"QANOT_CONFIG": str(cfg_path)}), \
                 _patch_mcp_package(), \
                 _patch_mcp_manager():
                register_mcp_tools(
                    reg, config, None, adapter,
                    get_user_id=lambda: "42",
                    get_chat_id=lambda: 100,
                )
                result = await reg.execute("mcp_propose", {
                    "name": "srv1",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "pkg"],
                    "env": {"FOO": "bar"},
                    "source": "user message",
                    "reason": "test",
                })
                pid = json.loads(result)["proposal_id"]

                cb = _make_callback(user_id=42, data=f"mcp_approve:{pid}")
                with patch("qanot.tools.mcp_manage._trigger_restart") as restart_mock:
                    await handle_mcp_approve_callback(
                        adapter, config, cb, "approve", pid,
                    )
                restart_mock.assert_called_once()

            on_disk = json.loads(cfg_path.read_text())
            # Other fields untouched
            assert on_disk["provider"] == "anthropic"
            assert on_disk["custom_field"] == "keep-me"
            assert on_disk["bot_token"] == "test-bot-token"
            # New MCP entry present with marker
            assert len(on_disk["mcp_servers"]) == 1
            entry = on_disk["mcp_servers"][0]
            assert entry["name"] == "srv1"
            assert entry["source"] == AGENT_SOURCE_MARKER
            assert entry["proposed_by"] == "user message"
            assert entry["command"] == "npx"
            # In-memory config updated too
            assert len(config.mcp_servers) == 1
            # Proposal consumed
            assert pid not in adapter._pending_mcp_proposals

    @pytest.mark.asyncio
    async def test_ttl_expiry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg_path = _write_config_file(tmp)
            config = _make_config(tmpdir)
            reg = ToolRegistry()
            adapter = _make_adapter()

            with patch.dict("os.environ", {"QANOT_CONFIG": str(cfg_path)}), \
                 _patch_mcp_package(), \
                 _patch_mcp_manager():
                register_mcp_tools(
                    reg, config, None, adapter,
                    get_user_id=lambda: "42",
                    get_chat_id=lambda: 100,
                )
                result = await reg.execute("mcp_propose", {
                    "name": "srv1",
                    "transport": "stdio",
                    "command": "npx",
                    "args": [],
                    "source": "user message",
                    "reason": "test",
                })
                pid = json.loads(result)["proposal_id"]

                # Force expire
                adapter._pending_mcp_proposals[pid]["expires_at"] = 0

                cb = _make_callback(user_id=42, data=f"mcp_approve:{pid}")
                with patch("qanot.tools.mcp_manage._trigger_restart") as restart_mock:
                    await handle_mcp_approve_callback(
                        adapter, config, cb, "approve", pid,
                    )
                restart_mock.assert_not_called()

            on_disk = json.loads(cfg_path.read_text())
            assert on_disk["mcp_servers"] == []


class TestMcpRemove:

    @pytest.mark.asyncio
    async def test_refuses_manual_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manual_entry = {"name": "manual-srv", "transport": "stdio", "command": "npx"}
            cfg_path = _write_config_file(tmp, mcp_servers=[manual_entry])
            config = _make_config(tmpdir, mcp_servers=[manual_entry])
            reg = ToolRegistry()
            adapter = _make_adapter()

            with patch.dict("os.environ", {"QANOT_CONFIG": str(cfg_path)}), \
                 _patch_mcp_package(), \
                 _patch_mcp_manager():
                register_mcp_tools(
                    reg, config, None, adapter,
                    get_user_id=lambda: "42",
                    get_chat_id=lambda: 100,
                )
                result = await reg.execute("mcp_remove", {
                    "name": "manual-srv",
                    "reason": "test",
                })

            data = json.loads(result)
            assert data["success"] is False
            assert "manually" in data["error"]
            assert adapter._pending_mcp_removals == {}

    @pytest.mark.asyncio
    async def test_removes_agent_entry_on_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            auto_entry = {
                "name": "auto-srv",
                "transport": "stdio",
                "command": "npx",
                "source": AGENT_SOURCE_MARKER,
                "proposed_by": "user message",
            }
            cfg_path = _write_config_file(tmp, mcp_servers=[auto_entry])
            config = _make_config(tmpdir, mcp_servers=[auto_entry])
            reg = ToolRegistry()
            adapter = _make_adapter()

            with patch.dict("os.environ", {"QANOT_CONFIG": str(cfg_path)}), \
                 _patch_mcp_package(), \
                 _patch_mcp_manager():
                register_mcp_tools(
                    reg, config, None, adapter,
                    get_user_id=lambda: "42",
                    get_chat_id=lambda: 100,
                )
                result = await reg.execute("mcp_remove", {
                    "name": "auto-srv",
                    "reason": "no longer needed",
                })
                pid = json.loads(result)["proposal_id"]

                cb = _make_callback(user_id=42, data=f"mcp_remove_approve:{pid}")
                with patch("qanot.tools.mcp_manage._trigger_restart") as restart_mock:
                    await handle_mcp_remove_callback(
                        adapter, config, cb, "approve", pid,
                    )
                restart_mock.assert_called_once()

            on_disk = json.loads(cfg_path.read_text())
            assert on_disk["mcp_servers"] == []
            assert config.mcp_servers == []


class TestMcpList:

    @pytest.mark.asyncio
    async def test_reports_configured_connected_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir, mcp_servers=[
                {"name": "srv-a", "transport": "stdio", "command": "npx"},
                {"name": "srv-b", "transport": "sse", "url": "https://x", "source": AGENT_SOURCE_MARKER},
            ])
            reg = ToolRegistry()
            adapter = _make_adapter()

            # Fake manager with one connected + one failed
            fake_server = SimpleNamespace(tools=[{"name": "t1"}, {"name": "t2"}], transport="stdio")
            fake_mgr = SimpleNamespace(
                connected_servers=["srv-a"],
                failed_servers=["srv-b"],
                _servers={"srv-a": fake_server},
            )

            with _patch_mcp_package():
                register_mcp_tools(
                    reg, config, fake_mgr, adapter,
                    get_user_id=lambda: "42",
                    get_chat_id=lambda: 100,
                )
                result = await reg.execute("mcp_list", {})

            data = json.loads(result)
            names = {c["name"] for c in data["configured"]}
            assert names == {"srv-a", "srv-b"}
            sources = {c["name"]: c["source"] for c in data["configured"]}
            assert sources["srv-a"] == "manual"
            assert sources["srv-b"] == AGENT_SOURCE_MARKER
            assert data["connected"][0]["name"] == "srv-a"
            assert data["connected"][0]["tool_count"] == 2
            assert data["failed"] == ["srv-b"]


class TestEnvVarResolution:

    def test_env_placeholder_resolved(self):
        from qanot.mcp_client import _resolve_env_dict
        import os

        os.environ["QANOT_TEST_VAR"] = "my-secret-value"
        try:
            resolved = _resolve_env_dict({"TOKEN": "${QANOT_TEST_VAR}", "OTHER": "plain"})
            assert resolved["TOKEN"] == "my-secret-value"
            assert resolved["OTHER"] == "plain"
        finally:
            del os.environ["QANOT_TEST_VAR"]

    def test_env_masked_in_approval_card(self):
        from qanot.tools.mcp_manage import _format_approval_card
        cfg = {
            "name": "srv",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "pkg"],
            "env": {"DATABASE_URL": "postgresql://user:password@host/db"},
        }
        card = _format_approval_card(cfg, "user message", "reason", [], False)
        # Plaintext value must not appear
        assert "password" not in card
        assert "postgresql" not in card
        assert "DATABASE_URL=***" in card


class TestBackoffRetry:

    @pytest.mark.asyncio
    async def test_retries_until_success(self):
        from qanot.mcp_client import MCPServerConnection

        attempts = []

        async def fake_connect_once(self):
            attempts.append(1)
            return len(attempts) >= 5  # fail 4 times, succeed on 5th

        # No sleep during test
        async def _no_sleep(_):
            return None

        with patch.object(MCPServerConnection, "_connect_once", fake_connect_once), \
             patch("qanot.mcp_client.asyncio.sleep", _no_sleep):
            conn = MCPServerConnection(name="x", command="npx", args=[])
            ok = await conn.connect(max_attempts=5)

        assert ok is True
        assert len(attempts) == 5

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(self):
        from qanot.mcp_client import MCPServerConnection

        attempts = []

        async def fake_connect_once(self):
            attempts.append(1)
            return False

        async def _no_sleep(_):
            return None

        with patch.object(MCPServerConnection, "_connect_once", fake_connect_once), \
             patch("qanot.mcp_client.asyncio.sleep", _no_sleep):
            conn = MCPServerConnection(name="x", command="npx", args=[])
            ok = await conn.connect(max_attempts=5)

        assert ok is False
        assert len(attempts) == 5


class TestIdempotentRegistration:

    def test_double_register_is_noop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _make_config(tmpdir)
            reg = ToolRegistry()
            adapter = _make_adapter()

            with _patch_mcp_package():
                register_mcp_tools(
                    reg, config, None, adapter,
                    get_user_id=lambda: "42",
                    get_chat_id=lambda: 100,
                )
                first = set(reg.tool_names)
                # Second call should not error and not duplicate
                register_mcp_tools(
                    reg, config, None, adapter,
                    get_user_id=lambda: "42",
                    get_chat_id=lambda: 100,
                )
                second = set(reg.tool_names)

            assert first == second
            assert "mcp_test" in first
            assert "mcp_propose" in first
            assert "mcp_list" in first
            assert "mcp_remove" in first
