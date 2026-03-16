"""Tests for dynamic agent management (create/update/delete at runtime)."""

from __future__ import annotations

import json
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

from qanot.registry import ToolRegistry
from qanot.config import AgentDefinition, Config
from qanot.tools.agent_manager import (
    _sanitize_agent_id,
    _save_agents_to_config,
    _active_agent_bots,
    register_agent_manager_tools,
)


def _make_config(**overrides) -> Config:
    return Config(
        bot_token="test-bot-token",
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_key="test-key",
        workspace_dir=overrides.get("workspace_dir", "/tmp/test-workspace"),
        sessions_dir="/tmp/test-sessions",
        agents=overrides.get("agents", []),
    )


class TestSanitizeAgentId:

    def test_simple(self):
        assert _sanitize_agent_id("my-agent") == "my-agent"

    def test_uppercase(self):
        assert _sanitize_agent_id("My-Agent") == "my-agent"

    def test_spaces(self):
        assert _sanitize_agent_id("my agent bot") == "my-agent-bot"

    def test_special_chars(self):
        assert _sanitize_agent_id("agent@#$123") == "agent-123"

    def test_multiple_hyphens(self):
        assert _sanitize_agent_id("a---b") == "a-b"

    def test_empty_string(self):
        assert _sanitize_agent_id("") == "agent"

    def test_max_length(self):
        result = _sanitize_agent_id("a" * 100)
        assert len(result) == 32


class TestSaveAgentsToConfig:

    def test_saves_agents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps({
                "bot_token": "test",
                "agents": [],
            }))

            config = _make_config()
            config.agents = [
                AgentDefinition(id="bot1", name="Bot 1", prompt="test prompt"),
                AgentDefinition(id="bot2", name="Bot 2", bot_token="token2"),
            ]

            with patch.dict("os.environ", {"QANOT_CONFIG": str(config_path)}):
                _save_agents_to_config(config)

            saved = json.loads(config_path.read_text())
            assert len(saved["agents"]) == 2
            assert saved["agents"][0]["id"] == "bot1"
            assert saved["agents"][0]["name"] == "Bot 1"
            assert saved["agents"][1]["bot_token"] == "token2"

    def test_preserves_other_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps({
                "bot_token": "main-token",
                "provider": "anthropic",
                "custom_field": "keep-me",
                "agents": [],
            }))

            config = _make_config()
            config.agents = [AgentDefinition(id="test")]

            with patch.dict("os.environ", {"QANOT_CONFIG": str(config_path)}):
                _save_agents_to_config(config)

            saved = json.loads(config_path.read_text())
            assert saved["bot_token"] == "main-token"
            assert saved["custom_field"] == "keep-me"

    def test_omits_default_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps({"agents": []}))

            config = _make_config()
            config.agents = [AgentDefinition(id="minimal")]

            with patch.dict("os.environ", {"QANOT_CONFIG": str(config_path)}):
                _save_agents_to_config(config)

            saved = json.loads(config_path.read_text())
            agent = saved["agents"][0]
            assert agent == {"id": "minimal"}  # Only ID, no defaults


class TestCreateAgent:

    @pytest.mark.asyncio
    async def test_create_basic_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps({"agents": []}))

            config = _make_config(workspace_dir=tmpdir)

            reg = ToolRegistry()
            with patch.dict("os.environ", {"QANOT_CONFIG": str(config_path)}):
                register_agent_manager_tools(
                    reg, config, MagicMock(), ToolRegistry(),
                    get_user_id=lambda: "test",
                )

                result = await reg.execute("create_agent", {
                    "id": "my-bot",
                    "name": "My Bot",
                    "prompt": "You are a helpful bot.",
                })

            data = json.loads(result)
            assert data["status"] == "created"
            assert data["agent_id"] == "my-bot"
            assert len(config.agents) == 1

            # Check SOUL.md was created
            soul = Path(tmpdir) / "agents" / "my-bot" / "SOUL.md"
            assert soul.exists()
            assert soul.read_text() == "You are a helpful bot."

    @pytest.mark.asyncio
    async def test_duplicate_id_rejected(self):
        config = _make_config(agents=[AgentDefinition(id="existing")])

        reg = ToolRegistry()
        with patch("qanot.tools.agent_manager._save_agents_to_config"):
            register_agent_manager_tools(
                reg, config, MagicMock(), ToolRegistry(),
                get_user_id=lambda: "test",
            )

            result = await reg.execute("create_agent", {
                "id": "existing",
                "name": "Dupe",
            })

        data = json.loads(result)
        assert "error" in data
        assert "already exists" in data["error"]

    @pytest.mark.asyncio
    async def test_create_with_bot_token_launches_bot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps({"agents": []}))

            config = _make_config(workspace_dir=tmpdir)

            reg = ToolRegistry()
            with patch.dict("os.environ", {"QANOT_CONFIG": str(config_path)}):
                with patch("qanot.tools.agent_manager._hot_launch_agent_bot", new_callable=AsyncMock) as mock_launch:
                    register_agent_manager_tools(
                        reg, config, MagicMock(), ToolRegistry(),
                        get_user_id=lambda: "test",
                    )

                    result = await reg.execute("create_agent", {
                        "id": "telegram-bot",
                        "name": "TG Bot",
                        "bot_token": "123:ABC",
                    })

                    data = json.loads(result)
                    assert data["has_bot"] is True
                    mock_launch.assert_called_once()


class TestUpdateAgent:

    @pytest.mark.asyncio
    async def test_update_name_and_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps({"agents": [{"id": "bot1", "name": "Old"}]}))

            config = _make_config(
                workspace_dir=tmpdir,
                agents=[AgentDefinition(id="bot1", name="Old", prompt="old prompt")],
            )

            reg = ToolRegistry()
            with patch.dict("os.environ", {"QANOT_CONFIG": str(config_path)}):
                register_agent_manager_tools(
                    reg, config, MagicMock(), ToolRegistry(),
                    get_user_id=lambda: "test",
                )

                result = await reg.execute("update_agent", {
                    "id": "bot1",
                    "name": "New Name",
                    "prompt": "new prompt",
                })

            data = json.loads(result)
            assert data["status"] == "updated"
            assert "name" in data["changes"]
            assert "prompt" in data["changes"]
            assert config.agents[0].name == "New Name"

    @pytest.mark.asyncio
    async def test_update_nonexistent(self):
        config = _make_config()

        reg = ToolRegistry()
        with patch("qanot.tools.agent_manager._save_agents_to_config"):
            register_agent_manager_tools(
                reg, config, MagicMock(), ToolRegistry(),
                get_user_id=lambda: "test",
            )

            result = await reg.execute("update_agent", {"id": "nope", "name": "X"})

        data = json.loads(result)
        assert "error" in data


class TestDeleteAgent:

    @pytest.mark.asyncio
    async def test_delete_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps({"agents": [{"id": "bot1"}]}))

            config = _make_config(
                workspace_dir=tmpdir,
                agents=[AgentDefinition(id="bot1", name="Bot 1")],
            )

            reg = ToolRegistry()
            with patch.dict("os.environ", {"QANOT_CONFIG": str(config_path)}):
                register_agent_manager_tools(
                    reg, config, MagicMock(), ToolRegistry(),
                    get_user_id=lambda: "test",
                )

                result = await reg.execute("delete_agent", {"id": "bot1"})

            data = json.loads(result)
            assert data["status"] == "deleted"
            assert len(config.agents) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        config = _make_config()

        reg = ToolRegistry()
        with patch("qanot.tools.agent_manager._save_agents_to_config"):
            register_agent_manager_tools(
                reg, config, MagicMock(), ToolRegistry(),
                get_user_id=lambda: "test",
            )

            result = await reg.execute("delete_agent", {"id": "nope"})

        data = json.loads(result)
        assert "error" in data


class TestToolRegistration:

    def test_all_management_tools_registered(self):
        config = _make_config()
        reg = ToolRegistry()

        with patch("qanot.tools.agent_manager._save_agents_to_config"):
            register_agent_manager_tools(
                reg, config, MagicMock(), ToolRegistry(),
                get_user_id=lambda: "test",
            )

        names = [t["name"] for t in reg.get_definitions()]
        assert "create_agent" in names
        assert "update_agent" in names
        assert "delete_agent" in names
        assert "restart_self" in names
