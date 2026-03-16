"""Tests for per-agent Telegram bot (multi-bot architecture)."""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from qanot.registry import ToolRegistry
from qanot.config import AgentDefinition, Config
from qanot.agent_bot import AgentBot, start_agent_bots


def _make_config(**overrides) -> MagicMock:
    config = MagicMock(spec=Config)
    config.agents = overrides.get("agents", [])
    config.provider = "anthropic"
    config.model = "claude-sonnet-4-6"
    config.api_key = "test-key"
    config.workspace_dir = overrides.get("workspace_dir", "/tmp/test-workspace")
    config.sessions_dir = "/tmp/test-sessions"
    config.max_context_tokens = 200000
    config.allowed_users = overrides.get("allowed_users", [])
    config.bot_token = "main-bot-token"
    config.timezone = "Asia/Tashkent"
    config.owner_name = ""
    config.bot_name = ""
    return config


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()

    async def noop(params):
        return "{}"

    for name in ["read_file", "write_file", "web_search"]:
        reg.register(name, f"desc for {name}", {"type": "object", "properties": {}}, noop)
    return reg


def _make_agent_def(**overrides) -> AgentDefinition:
    defaults = {
        "id": "test-agent",
        "name": "Test Agent",
        "prompt": "You are a test agent.",
        "bot_token": "test-agent-bot-token",
    }
    defaults.update(overrides)
    return AgentDefinition(**defaults)


class TestAgentBotInit:

    def test_creates_with_bot_token(self):
        agent_def = _make_agent_def()
        config = _make_config()
        provider = MagicMock()
        registry = _make_registry()

        with patch("qanot.agent_bot.Bot"):
            bot = AgentBot(agent_def, config, provider, registry)
            assert bot.agent_def.id == "test-agent"
            assert bot.agent_def.bot_token == "test-agent-bot-token"

    def test_allowed_users_empty_allows_all(self):
        config = _make_config(allowed_users=[])
        agent_def = _make_agent_def()

        with patch("qanot.agent_bot.Bot"):
            bot = AgentBot(agent_def, config, MagicMock(), _make_registry())
            assert bot._is_allowed(12345) is True

    def test_allowed_users_restricts(self):
        config = _make_config(allowed_users=[111, 222])
        agent_def = _make_agent_def()

        with patch("qanot.agent_bot.Bot"):
            bot = AgentBot(agent_def, config, MagicMock(), _make_registry())
            assert bot._is_allowed(111) is True
            assert bot._is_allowed(999) is False


class TestAgentBotToolRegistry:

    def test_builds_filtered_registry(self):
        agent_def = _make_agent_def(tools_allow=["read_file"])
        config = _make_config()
        registry = _make_registry()

        with patch("qanot.agent_bot.Bot"):
            bot = AgentBot(agent_def, config, MagicMock(), registry)
            child_reg = bot._build_tool_registry()
            names = [t["name"] for t in child_reg.get_definitions()]
            assert "read_file" in names
            assert "write_file" not in names
            assert "web_search" not in names

    def test_deny_list(self):
        agent_def = _make_agent_def(tools_deny=["web_search"])
        config = _make_config()
        registry = _make_registry()

        with patch("qanot.agent_bot.Bot"):
            bot = AgentBot(agent_def, config, MagicMock(), registry)
            child_reg = bot._build_tool_registry()
            names = [t["name"] for t in child_reg.get_definitions()]
            assert "read_file" in names
            assert "write_file" in names
            assert "web_search" not in names

    def test_no_filter_allows_all(self):
        agent_def = _make_agent_def()
        config = _make_config()
        registry = _make_registry()

        with patch("qanot.agent_bot.Bot"):
            bot = AgentBot(agent_def, config, MagicMock(), registry)
            child_reg = bot._build_tool_registry()
            names = [t["name"] for t in child_reg.get_definitions()]
            assert len(names) == 3  # All parent tools


class TestAgentBotProvider:

    def test_reuses_main_provider_when_no_overrides(self):
        agent_def = _make_agent_def(model="", provider="")
        config = _make_config()
        main_provider = MagicMock()

        with patch("qanot.agent_bot.Bot"):
            bot = AgentBot(agent_def, config, main_provider, _make_registry())
            result = bot._create_agent_provider()
            assert result is main_provider

    def test_creates_new_provider_with_model_override(self):
        agent_def = _make_agent_def(model="gpt-4o", provider="openai")
        config = _make_config()
        main_provider = MagicMock()

        with patch("qanot.agent_bot.Bot"):
            bot = AgentBot(agent_def, config, main_provider, _make_registry())
            with patch("qanot.providers.failover._create_single_provider") as mock_create:
                mock_create.return_value = MagicMock()
                result = bot._create_agent_provider()
                assert result is not main_provider
                mock_create.assert_called_once()


class TestAgentBotConfig:

    def test_makes_agent_config_with_overrides(self):
        agent_def = _make_agent_def(model="gpt-4o", provider="openai", name="GPT Agent")
        config = _make_config()
        # Need a real Config for dataclasses.replace
        real_config = Config(
            bot_token="main-token",
            provider="anthropic",
            model="claude-sonnet-4-6",
            api_key="test-key",
        )

        with patch("qanot.agent_bot.Bot"):
            bot = AgentBot(agent_def, real_config, MagicMock(), _make_registry())
            # Override with real config for this test
            bot.config = real_config
            agent_config = bot._make_agent_config()
            assert agent_config.model == "gpt-4o"
            assert agent_config.provider == "openai"
            assert agent_config.bot_name == "GPT Agent"


class TestStartAgentBots:

    @pytest.mark.asyncio
    async def test_skips_agents_without_bot_token(self):
        agents = [
            AgentDefinition(id="internal", name="Internal", prompt="test"),
            AgentDefinition(id="internal2", name="Internal2", prompt="test2"),
        ]
        config = _make_config(agents=agents)

        bots = await start_agent_bots(config, MagicMock(), _make_registry())
        assert len(bots) == 0

    @pytest.mark.asyncio
    async def test_starts_agents_with_bot_token(self):
        agents = [
            AgentDefinition(id="bot1", name="Bot 1", prompt="test", bot_token="token1"),
            AgentDefinition(id="internal", name="Internal", prompt="test"),
            AgentDefinition(id="bot2", name="Bot 2", prompt="test", bot_token="token2"),
        ]
        config = _make_config(agents=agents)

        with patch("qanot.agent_bot.Bot"):
            with patch("qanot.agent_bot.AgentBot.start", new_callable=AsyncMock):
                bots = await start_agent_bots(config, MagicMock(), _make_registry())
                assert len(bots) == 2
                assert bots[0].agent_def.id == "bot1"
                assert bots[1].agent_def.id == "bot2"

    @pytest.mark.asyncio
    async def test_empty_agents_list(self):
        config = _make_config(agents=[])
        bots = await start_agent_bots(config, MagicMock(), _make_registry())
        assert len(bots) == 0


class TestAgentBotSendResponse:

    @pytest.mark.asyncio
    async def test_sends_short_message(self):
        agent_def = _make_agent_def()
        config = _make_config()

        with patch("qanot.agent_bot.Bot") as MockBot:
            mock_bot_instance = MagicMock()
            mock_bot_instance.send_message = AsyncMock()
            MockBot.return_value = mock_bot_instance

            bot = AgentBot(agent_def, config, MagicMock(), _make_registry())
            await bot._send_response(123, "Hello world")
            mock_bot_instance.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_splits_long_message(self):
        agent_def = _make_agent_def()
        config = _make_config()

        with patch("qanot.agent_bot.Bot") as MockBot:
            mock_bot_instance = MagicMock()
            mock_bot_instance.send_message = AsyncMock()
            MockBot.return_value = mock_bot_instance

            bot = AgentBot(agent_def, config, MagicMock(), _make_registry())
            long_text = "x" * 8000
            await bot._send_response(123, long_text)
            assert mock_bot_instance.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_empty_message(self):
        agent_def = _make_agent_def()
        config = _make_config()

        with patch("qanot.agent_bot.Bot") as MockBot:
            mock_bot_instance = MagicMock()
            mock_bot_instance.send_message = AsyncMock()
            MockBot.return_value = mock_bot_instance

            bot = AgentBot(agent_def, config, MagicMock(), _make_registry())
            await bot._send_response(123, "")
            mock_bot_instance.send_message.assert_not_called()


class TestImportFix:
    """Verify the module-level import from providers works."""

    def test_import_create_single_provider(self):
        """Ensure _create_single_provider can be imported."""
        from qanot.providers.failover import _create_single_provider, ProviderProfile
        assert callable(_create_single_provider)
