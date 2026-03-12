"""Tests for config loader."""

from __future__ import annotations

import json
import pytest

from qanot.config import load_config, Config, PluginConfig, ProviderConfig


class TestConfig:
    def test_load_minimal(self, tmp_path):
        cfg = {"bot_token": "123:ABC", "api_key": "sk-test"}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(cfg))

        config = load_config(str(config_path))
        assert config.bot_token == "123:ABC"
        assert config.api_key == "sk-test"
        assert config.provider == "anthropic"  # default

    def test_load_with_plugins(self, tmp_path):
        cfg = {
            "bot_token": "t",
            "api_key": "k",
            "plugins": [
                {"name": "mysql_query", "enabled": True, "config": {"db_host": "localhost"}},
                {"name": "disabled_one", "enabled": False},
                "simple_plugin",
            ],
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(cfg))

        config = load_config(str(config_path))
        assert len(config.plugins) == 3
        assert config.plugins[0].name == "mysql_query"
        assert config.plugins[0].config["db_host"] == "localhost"
        assert config.plugins[1].enabled is False
        assert config.plugins[2].name == "simple_plugin"

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.json")

    def test_defaults(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")

        config = load_config(str(config_path))
        assert config.max_concurrent == 4
        assert config.max_context_tokens == 200000
        assert config.timezone == "Asia/Tashkent"
        assert config.allowed_users == []


    def test_load_multi_provider(self, tmp_path):
        cfg = {
            "bot_token": "t",
            "api_key": "sk-test",
            "providers": [
                {"name": "main", "provider": "anthropic", "model": "claude-sonnet-4-6", "api_key": "sk-1"},
                {"name": "backup", "provider": "gemini", "model": "gemini-2.5-flash", "api_key": "ai-2", "base_url": "https://custom.api"},
            ],
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(cfg))

        config = load_config(str(config_path))
        assert len(config.providers) == 2
        assert config.providers[0].name == "main"
        assert config.providers[0].provider == "anthropic"
        assert config.providers[1].name == "backup"
        assert config.providers[1].base_url == "https://custom.api"


class TestConfigDataclass:
    def test_default_values(self):
        config = Config()
        assert config.provider == "anthropic"
        assert config.model == "claude-sonnet-4-6"
        assert config.max_context_tokens == 200000
